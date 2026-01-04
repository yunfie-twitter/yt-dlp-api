import asyncio
import random
import math
import httpx
from urllib.parse import quote, unquote, urljoin, urlparse
from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, Response, JSONResponse
from starlette.background import BackgroundTask
import uuid

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
MANIFEST_TTL = 300 # 5 minutes cache for generated manifests

# CORS Headers
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
}

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


# Reduced timeout for faster response
client = httpx.AsyncClient(follow_redirects=True, timeout=10.0)


@router.on_event("shutdown")
async def shutdown_event():
    await client.aclose()


async def fetch_with_retry(
    url: str,
    request: Request | None,
    max_retry: int = 2,
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
    """Background prefetch - does not block response."""
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
        "#EXT-X-INDEPENDENT-SEGMENTS",  # Important for Video.js
        f"#EXT-X-TARGETDURATION:{int(SABR_SEGMENT_DURATION) + 1}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]

    for i in range(total_segments):
        lines.append(f"#EXTINF:{SABR_SEGMENT_DURATION:.3f},")
        lines.append(f"{segment_base}&seq={i}")

    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


@router.options("/stream")
@router.options("/stream/manifest/{manifest_id}")
@router.options("/stream/segment")
@router.options("/proxy")
async def cors_preflight():
    """Handle CORS preflight requests."""
    return Response(status_code=200, headers=CORS_HEADERS)


@router.get("/stream/manifest/{manifest_id}")
async def get_cached_manifest(manifest_id: str):
    """Serve cached rewritten manifest."""
    key = f"manifest:{manifest_id}"
    cached = load_cache(key, MANIFEST_TTL)
    
    if not cached:
        raise HTTPException(status_code=404, detail="Manifest expired or not found")
        
    body, meta = cached
    
    # Explicit CORS headers for manifest
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Expose-Headers": "*",
        "Cache-Control": "public, max-age=30",
        "Content-Disposition": "inline; filename=playlist.m3u8",
    }
    
    return Response(
        content=body,
        status_code=200,
        media_type="application/x-mpegurl",
        headers=headers
    )


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
        raise HTTPException(status_code=416, detail="Range not satisfiable")
        
    if end_byte >= total:
        end_byte = total - 1

    range_header = f"bytes={start_byte}-{end_byte}"
    
    r = await fetch_with_retry(
        target_url, 
        request, 
        max_retry=2,
        headers={"Range": range_header}
    )
    
    if not r:
        raise HTTPException(status_code=502, detail="Segment fetch failed")
        
    if r.status_code not in (200, 206):
        await r.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream error: {r.status_code}")

    # Detect actual content type from upstream
    content_type = r.headers.get("content-type", "video/mp4")
    
    if "octet-stream" in content_type:
        if ".mp4" in target_url or ".m4s" in target_url:
            content_type = "video/mp4"
        elif ".webm" in target_url:
            content_type = "video/webm"
        else:
            content_type = "video/mp2t"

    async def close_upstream():
        await r.aclose()

    # Explicit CORS headers for segments
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
        "Accept-Ranges": "bytes",
        "Content-Range": f"bytes {start_byte}-{end_byte}/{total}",
    }
    
    if "content-length" in r.headers:
        headers["Content-Length"] = r.headers["content-length"]

    return StreamingResponse(
        r.aiter_bytes(),
        status_code=206,  # Partial Content
        media_type=content_type,
        headers=headers,
        background=BackgroundTask(close_upstream)
    )


@router.post("/stream", dependencies=[Depends(rate_limiter)])
async def get_stream_playlist(request: Request, video_request: InfoRequest):
    """Get HLS playlist URL. Returns JSON with url to manifest."""

    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)

    validation_result = await SecurityValidator.validate_url(str(video_request.url))
    if validation_result == UrlValidationResult.BLOCKED:
        raise HTTPException(status_code=403, detail=_("error.private_ip"))
    if validation_result == UrlValidationResult.INVALID:
        raise HTTPException(status_code=400, detail=_("error.invalid_url"))

    # 1. Get Info (JSON)
    cmd = YTDLPCommandBuilder.build_info_command(str(video_request.url))
    try:
        result = await SubprocessExecutor.run(cmd, timeout=30.0)
        if result.returncode != 0:
             raise Exception("Info extraction failed")
        
        info = json.loads(result.stdout.decode())
        
        m3u8_url = None
        formats = info.get('formats', [])
        best_video = None
        is_native_m3u8 = False
        
        # Check if format is native m3u8
        for f in formats:
            if 'm3u8' in f.get('protocol', '') or f.get('ext') == 'm3u8':
                m3u8_url = f.get('url')
                is_native_m3u8 = True
                break
        
        if not m3u8_url:
            m3u8_url = info.get('manifest_url')
            if m3u8_url and '.m3u8' in m3u8_url:
                is_native_m3u8 = True

        if not m3u8_url:
            candidates = [f for f in formats if f.get('filesize') and f.get('vcodec') != 'none']
            if candidates:
                best_video = sorted(candidates, key=lambda x: x.get('tbr', 0) or 0, reverse=True)[0]
                m3u8_url = best_video['url']
            else:
                 m3u8_url = info.get('url')

        if not m3u8_url:
             raise HTTPException(status_code=500, detail="No stream URL found")

    except Exception as e:
        log_error(request, f"Get URL error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    base_url = str(request.base_url).rstrip("/")
    proxy_endpoint = f"{base_url}/proxy"
    
    final_content = b""
    use_sabr = False

    # 2. Only fetch m3u8 if it's explicitly a native m3u8 stream
    if is_native_m3u8:
        try:
            r = await fetch_with_retry(m3u8_url, request, max_retry=2)
            if r and r.status_code == 200:
                body = await r.aread()
                await r.aclose()
                
                ct = r.headers.get("content-type", "").lower()
                if "mpegurl" in ct or body[:7] == b"#EXTM3U":
                    new_content = rewrite_m3u8(body, m3u8_url, proxy_endpoint)
                    segments = extract_segments(body, m3u8_url, limit=3)
                    if segments:
                        # Fire and forget - don't await
                        asyncio.create_task(prefetch_segments(segments))
                    final_content = new_content
                else:
                    use_sabr = True
            else:
                if r:
                    await r.aclose()
                use_sabr = True
        except Exception:
            use_sabr = True
    else:
        # Skip m3u8 fetch for non-native streams to save time
        use_sabr = True

    # 3. SABR Fallback
    if use_sabr:
        log_info(request, f"Using SABR for {m3u8_url}")
        
        duration = info.get('duration', 0)
        if best_video:
            filesize = best_video.get('filesize') or best_video.get('filesize_approx') or 0
        else:
            filesize = info.get('filesize') or info.get('filesize_approx') or 0
            
        if filesize == 0:
            # Last resort: direct proxy
            encoded_url = quote(m3u8_url, safe="")
            proxy_url = f"{proxy_endpoint}?url={encoded_url}"
            final_content = (
                "#EXTM3U\n"
                "#EXT-X-VERSION:3\n"
                "#EXT-X-INDEPENDENT-SEGMENTS\n"
                "#EXT-X-TARGETDURATION:10\n"
                "#EXTINF:10.0,\n"
                f"{proxy_url}\n"
                "#EXT-X-ENDLIST\n"
            ).encode("utf-8")
        else:
            playlist_str = generate_sabr_playlist(m3u8_url, duration, filesize, base_url)
            final_content = playlist_str.encode("utf-8")
            
            # Debug log the generated manifest
            log_info(request, f"Generated SABR manifest ({len(final_content)} bytes, {duration}s, {filesize} bytes)")

    # 4. Save to cache and return URL
    manifest_id = str(uuid.uuid4())
    key = f"manifest:{manifest_id}"
    
    save_cache(
        normalized_url=key,
        body=final_content,
        status_code=200,
        content_type="application/x-mpegurl",
        ttl=MANIFEST_TTL
    )
    
    manifest_url = f"{base_url}/stream/manifest/{manifest_id}"
    
    response = JSONResponse({
        "url": manifest_url,
        "original_url": str(video_request.url),
        "method": "sabr" if use_sabr else "hls"
    })
    
    for key, value in CORS_HEADERS.items():
        response.headers[key] = value
    
    return response


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
                
                headers = CORS_HEADERS.copy()
                headers.update({
                    "Cache-Control": f"public, max-age={ttl}",
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{len(body)}",
                    "Content-Length": str(len(partial)),
                })
                
                return Response(
                    content=partial,
                    status_code=206,
                    media_type=ct,
                    headers=headers,
                )

        headers = CORS_HEADERS.copy()
        headers.update({
            "Cache-Control": f"public, max-age={ttl}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(len(body)),
        })
        
        return Response(
            content=body,
            status_code=200,
            media_type=ct,
            headers=headers,
        )

    r = await fetch_with_retry(normalized, request, max_retry=2)
    
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
            
            headers = CORS_HEADERS.copy()
            headers.update({
                "Cache-Control": cache_control,
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(body)),
            })
            
            return Response(
                content=body,
                status_code=200,
                media_type=content_type or "application/octet-stream",
                headers=headers,
            )

    except Exception:
        pass

    headers = CORS_HEADERS.copy()
    headers.update({
        "Cache-Control": cache_control,
        "Accept-Ranges": "bytes",
    })

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