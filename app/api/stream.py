import asyncio
import random
import math
import httpx
from urllib.parse import quote, unquote, urljoin, urlparse
from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from starlette.background import BackgroundTask

from app.models.request import InfoRequest
from app.services.ytdlp import YTDLPCommandBuilder, SubprocessExecutor
from app.core.security import SecurityValidator, UrlValidationResult
from app.core.logging import log_error, log_info
from app.infra.rate_limit import rate_limiter
from app.utils.locale import get_locale
from app.i18n import i18n
from app.utils.http_cache import (
    CACHE_POLICY,
    MAX_CACHE_BYTES,
    cache_ttl_for_url,
    load_cache,
    normalize_url,
    parse_range_header,
    save_cache,
)
import functools
import json

router = APIRouter()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

BLOCKED_HEADERS = {
    "x-forwarded-for",
    "x-real-ip",
    "client-ip",
    "cf-ray",
    "cf-connecting-ip",
    "true-client-ip",
    "via",
    "forwarded",
}

# SABR Settings
SABR_SEGMENT_DURATION = 5.0  # seconds

def fingerprint_headers(target_url: str, request: Request | None, stage: int = 0) -> dict:
    parsed = urlparse(target_url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"

    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Referer": referer,
    }

    if request and "range" in request.headers:
        h["Range"] = request.headers["range"]

    if stage >= 1:
        h["Accept-Language"] = "en-GB,en;q=0.8"

    if stage >= 2:
        h.update({
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Dest": "video",
        })

    if stage >= 3:
        h["Range"] = "bytes=0-"

    return h


client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)


@router.on_event("shutdown")
async def shutdown_event():
    await client.aclose()


async def fetch_with_retry(
    url: str,
    request: Request | None,
    max_retry: int = 4,
    method: str = "GET",
    headers: dict | None = None
) -> httpx.Response | None:
    last_resp = None

    for stage in range(max_retry):
        req_headers = fingerprint_headers(url, request, stage=stage)
        if headers:
            req_headers.update(headers)

        try:
            req = client.build_request(method, url, headers=req_headers)
            r = await client.send(req, stream=True)

            if r.status_code != 403:
                return r

            await r.aclose()
            last_resp = r

        except Exception:
            break

    return last_resp


def extract_segments(m3u8_body: bytes, base_url: str, limit: int = 3) -> list[str]:
    try:
        text = m3u8_body.decode("utf-8", errors="ignore")
    except:
        return []

    urls = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        
        if not (stripped.endswith(".ts") or stripped.endswith(".m4s")):
            continue
            
        try:
            abs_url = urljoin(base_url, stripped)
            urls.append(abs_url)
            if len(urls) >= limit:
                break
        except:
            continue

    return urls


async def prefetch_segments(urls: list[str]):
    for u in urls:
        try:
            req = client.build_request(
                "GET",
                u,
                headers=fingerprint_headers(u, None, stage=0),
            )
            r = await client.send(req, stream=True)

            if r.status_code == 200 and "content-length" in r.headers:
                size_str = r.headers["content-length"]
                if size_str.isdigit():
                    size = int(size_str)
                    if size < MAX_CACHE_BYTES:
                        body = await r.aread()
                        ttl = cache_ttl_for_url(u)
                        if ttl > 5:
                            save_cache(
                                normalized_url=normalize_url(u),
                                body=body,
                                status_code=200,
                                content_type=r.headers.get("content-type"),
                                ttl=ttl,
                            )
            await r.aclose()
        except Exception:
            pass


def rewrite_m3u8(body: bytes, base_url: str, proxy_base: str) -> bytes:
    try:
        text = body.decode("utf-8", errors="ignore")
    except:
        return body

    lines = []
    for line in text.splitlines():
        stripped = line.rstrip("\r\n")
        if not stripped or stripped.startswith("#"):
            lines.append(stripped)
            continue

        try:
            abs_url = urljoin(base_url, stripped)
            proxied = f"{proxy_base}?url={quote(abs_url, safe='')}"
            lines.append(proxied)
        except:
            continue

    return ("\n".join(lines) + "\n").encode("utf-8")


# --- SABR Logic ---

def generate_sabr_playlist(
    url: str,
    duration: float,
    filesize: int,
    base_url: str
) -> str:
    """Generate pseudo-HLS playlist for progressive files."""
    
    # Calculate approximate segment size (bytes) based on duration
    # This assumes constant bitrate, which is not always true but good enough for fallback
    if duration <= 0:
        duration = 600 # Fallback 10 min
    
    total_segments = math.ceil(duration / SABR_SEGMENT_DURATION)
    avg_bytes_per_sec = filesize / duration
    bytes_per_segment = int(avg_bytes_per_sec * SABR_SEGMENT_DURATION)
    
    encoded_url = quote(url, safe="")
    segment_base = f"{base_url}/stream/segment?url={encoded_url}&bps={bytes_per_segment}&total={filesize}"

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{int(SABR_SEGMENT_DURATION)}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]

    for i in range(total_segments):
        lines.append(f"#EXTINF:{SABR_SEGMENT_DURATION:.3f},")
        lines.append(f"{segment_base}&seq={i}")

    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


@router.get("/stream/segment")
async def sabr_segment(
    request: Request,
    url: str = Query(...),
    seq: int = Query(...),
    bps: int = Query(...),
    total: int = Query(...)
):
    """Proxy specific byte range as a segment."""
    target_url = unquote(url)
    
    # Calculate Range
    start_byte = seq * bps
    end_byte = start_byte + bps - 1
    
    if start_byte >= total:
        raise HTTPException(status_code=404, detail="Segment out of range")
        
    if end_byte >= total:
        end_byte = total - 1

    range_header = f"bytes={start_byte}-{end_byte}"
    
    # Reuse fetch_with_retry with Range
    r = await fetch_with_retry(
        target_url, 
        request, 
        max_retry=3, 
        headers={"Range": range_header}
    )
    
    if not r:
        raise HTTPException(status_code=502, detail="Segment fetch failed")
        
    if r.status_code not in (200, 206):
        await r.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream error: {r.status_code}")

    async def close_upstream():
        await r.aclose()

    return StreamingResponse(
        r.aiter_bytes(),
        status_code=200, # HLS segments are usually 200 OK
        media_type="video/mp2t", # Determine dynamically ideally, but often TS/MP4
        background=BackgroundTask(close_upstream)
    )


@router.post("/stream", dependencies=[Depends(rate_limiter)])
async def get_stream_playlist(request: Request, video_request: InfoRequest):
    """Get HLS playlist. Tries direct m3u8 first, falls back to SABR."""

    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)

    validation_result = await SecurityValidator.validate_url(str(video_request.url))
    if validation_result == UrlValidationResult.BLOCKED:
        raise HTTPException(status_code=403, detail=_("error.private_ip"))
    if validation_result == UrlValidationResult.INVALID:
        raise HTTPException(status_code=400, detail=_("error.invalid_url"))

    # 1. Get Info (JSON) to check available formats
    # We need duration and filesize for SABR
    cmd = YTDLPCommandBuilder.build_info_command(str(video_request.url))
    try:
        # We need JSON dump to get duration/filesize
        # 'build_info_command' uses -j
        result = await SubprocessExecutor.run(cmd, timeout=30.0)
        if result.returncode != 0:
             raise Exception("Info extraction failed")
        
        info = json.loads(result.stdout.decode())
        
        # Try to find m3u8
        m3u8_url = None
        # Check 'formats' for m3u8
        formats = info.get('formats', [])
        best_video = None
        
        # Simple selection: prefer m3u8, else best mp4/webm
        for f in formats:
            if 'm3u8' in f.get('protocol', '') or f.get('ext') == 'm3u8':
                m3u8_url = f.get('url')
                break
        
        if not m3u8_url:
            # If no m3u8 found in formats, maybe manifest_url exists
            m3u8_url = info.get('manifest_url')

        # Fallback candidate (best progressive)
        if not m3u8_url:
            # Filter for video files with filesize
            candidates = [f for f in formats if f.get('filesize') and f.get('vcodec') != 'none']
            if candidates:
                # Sort by bitrate/quality
                best_video = sorted(candidates, key=lambda x: x.get('tbr', 0) or 0, reverse=True)[0]
                m3u8_url = best_video['url'] # We'll try to treat this as m3u8 first, then fallback
            else:
                 # Last resort: 'url' field of info
                 m3u8_url = info.get('url')

        if not m3u8_url:
             raise HTTPException(status_code=500, detail="No stream URL found")

    except Exception as e:
        log_error(request, f"Get URL error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    base_url = str(request.base_url).rstrip("/")
    proxy_endpoint = f"{base_url}/proxy"

    # 2. Try to fetch as m3u8
    use_sabr = False
    try:
        r = await fetch_with_retry(m3u8_url, request)
        if not r or r.status_code != 200:
             if r: await r.aclose()
             use_sabr = True # Fallback if fetch fails
        else:
            body = await r.aread()
            await r.aclose()
            
            # Check content type / magic
            ct = r.headers.get("content-type", "").lower()
            if "mpegurl" in ct or body[:7] == b"#EXTM3U":
                 # It IS a playlist
                 new_content = rewrite_m3u8(body, m3u8_url, proxy_endpoint)
                 
                 segments = extract_segments(body, m3u8_url, limit=3)
                 if segments:
                     asyncio.create_task(prefetch_segments(segments))
                     
                 if b"#EXT-X-ENDLIST" in body:
                    cache_control = "public, max-age=30"
                 else:
                    cache_control = "no-cache"

                 return Response(
                     content=new_content, 
                     media_type="application/vnd.apple.mpegurl",
                     headers={"Cache-Control": cache_control}
                )
            else:
                # Not a playlist -> SABR Fallback
                use_sabr = True
    
    except Exception:
        use_sabr = True

    # 3. SABR Fallback Execution
    if use_sabr:
        log_info(request, f"Fallback to SABR for {m3u8_url}")
        
        # We need metadata for SABR
        duration = info.get('duration', 0)
        # If we selected a specific format (best_video), use its filesize
        if best_video:
            filesize = best_video.get('filesize') or best_video.get('filesize_approx') or 0
        else:
            filesize = info.get('filesize') or info.get('filesize_approx') or 0
            
        if filesize == 0:
            # If we can't get filesize, SABR can't calculate ranges properly
            # As a last last resort, wrap in simple m3u8 (original progressive proxy)
            encoded_url = quote(m3u8_url, safe="")
            proxy_url = f"{proxy_endpoint}?url={encoded_url}"
            new_content = (
                "#EXTM3U\n"
                "#EXT-X-VERSION:3\n"
                "#EXT-X-TARGETDURATION:0\n"
                "#EXTINF:10.0,\n"
                f"{proxy_url}\n"
                "#EXT-X-ENDLIST\n"
            ).encode("utf-8")
        else:
            # Generate pseudo-HLS
            playlist_str = generate_sabr_playlist(m3u8_url, duration, filesize, base_url)
            new_content = playlist_str.encode("utf-8")

        return Response(
             content=new_content, 
             media_type="application/vnd.apple.mpegurl",
             headers={"Cache-Control": "public, max-age=60"}
        )

    # Should not reach here
    raise HTTPException(status_code=500, detail="Unknown error")


@router.get("/proxy")
async def proxy(request: Request, url: str = Query(...)):
    """Proxy m3u8/ts/m4s (Range transparent + file cache)."""

    target_url = unquote(url)
    normalized = normalize_url(target_url)

    validation_result = await SecurityValidator.validate_url(normalized)
    if validation_result == UrlValidationResult.BLOCKED:
        raise HTTPException(status_code=403, detail="Access denied")
    if validation_result == UrlValidationResult.INVALID:
        raise HTTPException(status_code=400, detail="Invalid URL")

    ttl = cache_ttl_for_url(normalized)
    cached = load_cache(normalized, ttl)
    
    if cached is not None:
        body, meta = cached
        ct = meta.content_type or "application/octet-stream"

        range_header = request.headers.get("range")
        if range_header:
            rng = parse_range_header(range_header, len(body))
            if rng:
                start, end = rng
                partial = body[start : end + 1]
                return Response(
                    content=partial,
                    status_code=206,
                    media_type=ct,
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": f"public, max-age={ttl}",
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes {start}-{end}/{len(body)}",
                        "Content-Length": str(len(partial)),
                    },
                )

        return Response(
            content=body,
            status_code=200,
            media_type=ct,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": f"public, max-age={ttl}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(body)),
            },
        )

    r = await fetch_with_retry(normalized, request)
    
    if not r:
        raise HTTPException(status_code=502, detail="Proxy failed")
    
    if r.status_code == 403:
        await r.aclose()
        raise HTTPException(status_code=403, detail="Upstream forbidden")

    cache_control = f"public, max-age={ttl}"
    has_range = "range" in request.headers

    try:
        status_code = r.status_code
        content_type = r.headers.get("content-type")
        content_length = r.headers.get("content-length")
        content_length_int = int(content_length) if content_length and content_length.isdigit() else None

        if (
            status_code == 200
            and not has_range
            and content_length_int is not None
            and content_length_int < MAX_CACHE_BYTES
        ):
            body = await r.aread()
            await r.aclose()
            
            save_cache(
                normalized_url=normalized,
                body=body,
                status_code=status_code,
                content_type=content_type,
                ttl=ttl,
            )
            return Response(
                content=body,
                status_code=200,
                media_type=content_type or "application/octet-stream",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": cache_control,
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(len(body)),
                },
            )

    except Exception:
        pass

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": cache_control,
        "Accept-Ranges": "bytes",
    }

    for k in ("content-range", "content-length"):
        if k in r.headers:
            headers[k.title()] = r.headers[k]

    async def close_upstream():
        await r.aclose()

    return StreamingResponse(
        r.aiter_bytes(),
        status_code=r.status_code,
        media_type=r.headers.get("content-type"),
        headers=headers,
        background=BackgroundTask(close_upstream),
    )
