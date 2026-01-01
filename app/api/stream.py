import asyncio
import random
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

router = APIRouter()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# Blocked headers that should never be forwarded
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


def fingerprint_headers(target_url: str, request: Request | None, stage: int = 0) -> dict:
    """Generate headers with progressive fingerprinting stages.
    
    Args:
        target_url: The URL being requested
        request: FastAPI request (can be None for prefetch)
        stage: Retry stage (0-3) for progressive fingerprinting
    """
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

    # Range transparency (critical for seek)
    if request and "range" in request.headers:
        h["Range"] = request.headers["range"]

    # Progressive fingerprinting stages for 403 retry
    if stage >= 1:
        h["Accept-Language"] = "en-GB,en;q=0.8"

    if stage >= 2:
        h.update({
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Dest": "video",
        })

    if stage >= 3:
        # Force Range header as fallback
        h["Range"] = "bytes=0-"

    return h


# Reuse client for keep-alive
client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)


@router.on_event("shutdown")
async def shutdown_event():
    await client.aclose()


async def fetch_with_retry(
    url: str,
    request: Request | None,
    max_retry: int = 4,
) -> httpx.Response | None:
    """Fetch URL with progressive retry on 403.
    
    Returns:
        Response object or None if all retries failed
    """
    last_resp = None

    for stage in range(max_retry):
        headers = fingerprint_headers(url, request, stage=stage)

        try:
            req = client.build_request("GET", url, headers=headers)
            r = await client.send(req, stream=True)

            # Success or non-403 error - return immediately
            if r.status_code != 403:
                return r

            # 403 - close and retry with next stage
            await r.aclose()
            last_resp = r

        except Exception:
            # Connection error - stop retrying
            break

    return last_resp


def extract_segments(m3u8_body: bytes, base_url: str, limit: int = 3) -> list[str]:
    """Extract segment URLs from m3u8 playlist.
    
    Args:
        m3u8_body: Raw m3u8 content
        base_url: Base URL for resolving relative paths
        limit: Maximum number of segments to extract
    
    Returns:
        List of absolute segment URLs
    """
    try:
        text = m3u8_body.decode("utf-8", errors="ignore")
    except:
        return []

    urls = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        
        # Only prefetch actual segments (.ts, .m4s)
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
    """Prefetch segments in background for cache warming.
    
    Args:
        urls: List of segment URLs to prefetch
    """
    for u in urls:
        try:
            # No Range, cache-friendly request
            req = client.build_request(
                "GET",
                u,
                headers=fingerprint_headers(u, None, stage=0),
            )
            r = await client.send(req, stream=True)

            # Only cache small successful responses
            if r.status_code == 200 and "content-length" in r.headers:
                size_str = r.headers["content-length"]
                if size_str.isdigit():
                    size = int(size_str)
                    if size < MAX_CACHE_BYTES:
                        body = await r.aread()
                        ttl = cache_ttl_for_url(u)
                        if ttl > 5:  # Don't cache if TTL too short
                            save_cache(
                                normalized_url=normalize_url(u),
                                body=body,
                                status_code=200,
                                content_type=r.headers.get("content-type"),
                                ttl=ttl,
                            )
            await r.aclose()
        except Exception:
            # Prefetch failure is not critical, silently continue
            pass


def rewrite_m3u8(body: bytes, base_url: str, proxy_base: str) -> bytes:
    """Rewrite m3u8 playlist URLs to go through proxy.
    
    Args:
        body: Original m3u8 content
        base_url: Base URL for resolving relative paths
        proxy_base: Proxy endpoint base URL
    
    Returns:
        Rewritten m3u8 content
    """
    try:
        text = body.decode("utf-8", errors="ignore")
    except:
        return body

    lines = []
    for line in text.splitlines():
        stripped = line.rstrip("\r\n")
        
        # Keep comments and empty lines as-is
        if not stripped or stripped.startswith("#"):
            lines.append(stripped)
            continue

        # Rewrite URLs to proxy
        try:
            abs_url = urljoin(base_url, stripped)
            proxied = f"{proxy_base}?url={quote(abs_url, safe='')}"
            lines.append(proxied)
        except:
            # Skip malformed URLs
            continue

    return ("\n".join(lines) + "\n").encode("utf-8")


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

    # Proxy endpoint base
    base_url = str(request.base_url).rstrip("/")
    proxy_endpoint = f"{base_url}/proxy"

    # Fetch playlist with retry
    r = await fetch_with_retry(m3u8_url, request)
    if not r:
        raise HTTPException(status_code=502, detail="Failed to fetch upstream playlist")
    
    if r.status_code == 403:
        await r.aclose()
        raise HTTPException(status_code=403, detail="Upstream forbidden")
    
    if r.status_code != 200:
        await r.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream error: {r.status_code}")

    try:
        body = await r.aread()
        await r.aclose()
        
        # Check if it's actually a playlist
        ct = r.headers.get("content-type", "").lower()
        is_playlist = "mpegurl" in ct or "application/x-mpegurl" in ct or "vnd.apple.mpegurl" in ct
        if not is_playlist and body[:7] == b"#EXTM3U":
            is_playlist = True

        if not is_playlist:
            # Not a playlist - wrap in simple m3u8
            encoded_url = quote(m3u8_url, safe="")
            proxy_url = f"{proxy_endpoint}?url={encoded_url}"
            
            new_content = (
                "#EXTM3U\n"
                "#EXT-X-VERSION:3\n"
                "#EXT-X-TARGETDURATION:0\n"
                "#EXT-X-MEDIA-SEQUENCE:0\n"
                "#EXTINF:10.0,\n"
                f"{proxy_url}\n"
                "#EXT-X-ENDLIST\n"
            ).encode("utf-8")
            log_info(request, f"Wrapped direct stream in generated m3u8: {m3u8_url}")
        else:
            # Rewrite playlist URLs
            new_content = rewrite_m3u8(body, m3u8_url, proxy_endpoint)
            
            # Start prefetch in background (non-blocking)
            segments = extract_segments(body, m3u8_url, limit=3)
            if segments:
                asyncio.create_task(prefetch_segments(segments))

    except Exception as e:
        log_error(request, f"Process m3u8 error: {str(e)}")
        raise HTTPException(status_code=502, detail="Failed to process playlist")

    # Determine cache policy for m3u8
    # Live streams should not be cached
    if b"#EXT-X-ENDLIST" in body:
        cache_control = "public, max-age=30"
    else:
        cache_control = "no-cache"

    headers = {
        "Cache-Control": cache_control,
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

    # Cache miss: fetch with retry
    r = await fetch_with_retry(normalized, request)
    
    if not r:
        raise HTTPException(status_code=502, detail="Proxy failed")
    
    if r.status_code == 403:
        await r.aclose()
        raise HTTPException(status_code=403, detail="Upstream forbidden")

    # Decide Cache-Control by extension
    cache_control = f"public, max-age={ttl}"

    # If request was ranged, do not cache, just stream and pass through status.
    has_range = "range" in request.headers

    # Try caching only for small 200 responses without Range
    # Note: If content-length is missing (chunked encoding), we skip caching and stream
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
        # if cache attempt fails, fall back to streaming
        pass

    # Stream fallback (range transparent)
    # IMPORTANT: Use BackgroundTask to close response after streaming completes
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": cache_control,
        "Accept-Ranges": "bytes",
    }

    # Pass through upstream range-related headers when present
    for k in ("content-range", "content-length"):
        if k in r.headers:
            headers[k.title()] = r.headers[k]

    async def close_upstream():
        """Background task to close upstream connection after streaming."""
        await r.aclose()

    return StreamingResponse(
        r.aiter_bytes(),
        status_code=r.status_code,
        media_type=r.headers.get("content-type"),
        headers=headers,
        background=BackgroundTask(close_upstream),
    )
