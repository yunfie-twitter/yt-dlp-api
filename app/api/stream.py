import random
import httpx
from urllib.parse import quote, unquote, urljoin, urlparse
from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, Response

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

router = APIRouter()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

LANGS = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.8",
]


def fingerprint_headers(target_url: str, request: Request) -> dict:
    parsed = urlparse(target_url)
    referer = f"{parsed.scheme}://{parsed.netloc}/"

    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": random.choice(LANGS),
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
        "Referer": referer,
    }

    # Range transparency (critical for seek)
    range_header = request.headers.get("range")
    if range_header:
        h["Range"] = range_header

    return h


# Reuse client for keep-alive
client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)


@router.on_event("shutdown")
async def shutdown_event():
    await client.aclose()


@router.post("/stream", dependencies=[Depends(rate_limiter)])
async def get_stream_playlist(request: Request, video_request: InfoRequest):
    """Get HLS playlist with rewritten proxy URLs."""

    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)

    # SSRF check on input URL
    validation_result = await SecurityValidator.validate_url(str(video_request.url))
    if validation_result == UrlValidationResult.BLOCKED:
        raise HTTPException(status_code=403, detail=_("error.private_ip"))
    if validation_result == UrlValidationResult.INVALID:
        raise HTTPException(status_code=400, detail=_("error.invalid_url", reason="Invalid format"))

    # Get direct stream URL (m3u8 preferred)
    # Using YTDLPCommandBuilder.build_get_url_command which already requests 'best[protocol^=m3u8]/best'
    cmd = YTDLPCommandBuilder.build_get_url_command(str(video_request.url))
    try:
        result = await SubprocessExecutor.run(cmd, timeout=30.0)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail="Failed to get stream URL")
        
        urls = result.stdout.decode().strip().split("\n")
        m3u8_url = urls[0] if urls else None
        
        if not m3u8_url:
             raise HTTPException(status_code=500, detail="No stream URL found")
             
    except Exception as e:
        log_error(request, f"Get URL error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    # Rewrite segments/playlists to point to /proxy
    base_url = str(request.base_url).rstrip("/")
    # Proxy endpoint is at /proxy (not /stream/proxy) because router is included at root level for these paths
    proxy_endpoint = f"{base_url}/proxy"

    # Fetch playlist
    try:
        upstream_headers = fingerprint_headers(m3u8_url, request)
        resp = await client.get(m3u8_url, headers=upstream_headers)
        resp.raise_for_status()
        
        # Check if it's actually a playlist
        ct = resp.headers.get("content-type", "").lower()
        is_playlist = "mpegurl" in ct or "application/x-mpegurl" in ct or "vnd.apple.mpegurl" in ct
        if not is_playlist:
             first_line = resp.content[:7]
             if first_line == b"#EXTM3U":
                 is_playlist = True

        if not is_playlist:
            # If yt-dlp returned a direct file link (mp4/webm) instead of m3u8,
            # we wrap it in a simple m3u8 playlist so the client still gets a playlist as expected.
            # This handles cases where 'best[protocol^=m3u8]' fell back to 'best' (direct file).
            encoded_url = quote(m3u8_url, safe="")
            proxy_url = f"{proxy_endpoint}?url={encoded_url}"
            
            # Create a simple VOD playlist wrapping the single file
            new_content = (
                "#EXTM3U\n"
                "#EXT-X-VERSION:3\n"
                "#EXT-X-TARGETDURATION:0\n"
                "#EXT-X-MEDIA-SEQUENCE:0\n"
                "#EXTINF:10.0,\n"
                f"{proxy_url}\n"
                "#EXT-X-ENDLIST\n"
            )
            # Log this fallback
            log_info(request, f"Wrapped direct stream in generated m3u8: {m3u8_url}")
        else:
            # It is a playlist, rewrite it
            content = resp.text
            rewritten_lines: list[str] = []
            for raw_line in content.splitlines():
                line = raw_line.rstrip("\r\n")

                if line.startswith("#") or line.strip() == "":
                    rewritten_lines.append(line)
                    continue
                
                try:
                    absolute_url = urljoin(m3u8_url, line.strip())
                    encoded_url = quote(absolute_url, safe="")
                    rewritten_lines.append(f"{proxy_endpoint}?url={encoded_url}")
                except ValueError:
                    log_error(request, f"Skipping invalid URL in playlist: {line[:50]}...")
                    continue

            new_content = "\n".join(rewritten_lines) + "\n"

    except Exception as e:
        log_error(request, f"Fetch m3u8 error: {str(e)}")
        raise HTTPException(status_code=502, detail="Failed to fetch upstream playlist")

    headers = {
        "Cache-Control": "public, max-age=5",
        "Content-Disposition": "inline; filename=playlist.m3u8",
    }

    return Response(content=new_content, media_type="application/vnd.apple.mpegurl", headers=headers)


@router.get("/proxy")
async def proxy(request: Request, url: str = Query(...)):
    """Proxy m3u8/ts/m4s (Range transparent + file cache)."""

    target_url = unquote(url)

    # Normalize URL (cache key stability)
    normalized = normalize_url(target_url)

    # SSRF check on normalized URL
    validation_result = await SecurityValidator.validate_url(normalized)
    if validation_result == UrlValidationResult.BLOCKED:
        raise HTTPException(status_code=403, detail="Access denied")
    if validation_result == UrlValidationResult.INVALID:
        raise HTTPException(status_code=400, detail="Invalid URL")

    ttl = cache_ttl_for_url(normalized)

    # Cache hit?
    cached = load_cache(normalized, ttl)
    if cached is not None:
        body, meta = cached
        ct = meta.content_type or "application/octet-stream"

        # Range support for cached body
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

    # Cache miss: fetch upstream
    upstream_headers = fingerprint_headers(normalized, request)

    try:
        req = client.build_request("GET", normalized, headers=upstream_headers)
        r = await client.send(req, stream=True)
    except Exception as e:
        log_error(request, f"Proxy request error: {str(e)}")
        raise HTTPException(status_code=502, detail="Proxy failed")

    # Decide Cache-Control by extension
    cache_control = f"public, max-age={ttl}"

    # If request was ranged, do not cache, just stream and pass through status.
    has_range = "range" in request.headers

    # Try caching only for small 200 responses without Range
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
        # if cache attempt fails, fall back to streaming
        pass

    # Stream fallback (range transparent)
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": cache_control,
        "Accept-Ranges": "bytes",
    }

    # Pass through upstream range-related headers when present
    for k in ("content-range", "content-length"):
        if k in r.headers:
            headers[k.title()] = r.headers[k]

    return StreamingResponse(
        r.aiter_bytes(),
        status_code=r.status_code,
        media_type=r.headers.get("content-type"),
        headers=headers,
    )
