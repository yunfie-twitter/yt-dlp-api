import httpx
from urllib.parse import quote, unquote, urljoin
from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse, Response
from app.models.request import InfoRequest
from app.services.ytdlp import YTDLPCommandBuilder, SubprocessExecutor
from app.core.security import SecurityValidator, UrlValidationResult
from app.core.logging import log_info, log_error
from app.infra.rate_limit import rate_limiter
from app.utils.locale import get_locale
from app.i18n import i18n
import functools

router = APIRouter()

# Reuse client for keep-alive
client = httpx.AsyncClient(follow_redirects=True, timeout=30.0)

@router.on_event("shutdown")
async def shutdown_event():
    await client.aclose()

@router.post("/stream", dependencies=[Depends(rate_limiter)])
async def get_stream_playlist(request: Request, video_request: InfoRequest):
    """
    Get HLS playlist with rewritten proxy URLs.
    Returns the m3u8 content directly.
    """
    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)

    # 1. SSRF Check on input URL
    validation_result = await SecurityValidator.validate_url(str(video_request.url))
    if validation_result == UrlValidationResult.BLOCKED:
        raise HTTPException(status_code=403, detail=_("error.private_ip"))
    if validation_result == UrlValidationResult.INVALID:
        raise HTTPException(status_code=400, detail=_("error.invalid_url", reason="Invalid format"))

    # 2. Get direct stream URL (m3u8)
    cmd = YTDLPCommandBuilder.build_get_url_command(str(video_request.url))
    try:
        result = await SubprocessExecutor.run(cmd, timeout=30.0)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail="Failed to get stream URL")
        
        m3u8_url = result.stdout.decode().strip()
        # Handle cases where multiple URLs are returned (video + audio), take the first one or logic
        # yt-dlp -g might return two lines. For HLS proxy, we usually want the master manifest or the video manifest.
        m3u8_url = m3u8_url.split('\n')[0]
        
    except Exception as e:
        log_error(request, f"Get URL error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

    # 3. Fetch the m3u8 content
    try:
        resp = await client.get(m3u8_url)
        resp.raise_for_status()
        content = resp.text
    except Exception as e:
        log_error(request, f"Fetch m3u8 error: {str(e)}")
        raise HTTPException(status_code=502, detail="Failed to fetch upstream playlist")

    # 4. Rewrite segments to point to /proxy
    base_url = str(request.base_url).rstrip('/')
    proxy_endpoint = f"{base_url}/stream/proxy"
    
    rewritten_lines = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            rewritten_lines.append(line)
        else:
            # It's a URL (absolute or relative)
            absolute_url = urljoin(m3u8_url, line)
            encoded_url = quote(absolute_url)
            # Create proxy URL
            proxy_url = f"{proxy_endpoint}?url={encoded_url}"
            rewritten_lines.append(proxy_url)

    new_content = "\n".join(rewritten_lines)

    return Response(
        content=new_content,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-cache",
            "Content-Disposition": "inline; filename=playlist.m3u8"
        }
    )

@router.get("/proxy")
async def proxy_segment(request: Request, url: str = Query(...)):
    """
    Proxy stream segment or playlist.
    """
    # 1. SSRF Check on target URL
    # IMPORTANT: Since we are fetching arbitrary URLs, we MUST validate them.
    # However, ts segments from valid CDNs might be dynamic.
    # We rely on SecurityValidator to block private IPs.
    validation_result = await SecurityValidator.validate_url(url)
    if validation_result == UrlValidationResult.BLOCKED:
        raise HTTPException(status_code=403, detail="Access denied")
    if validation_result == UrlValidationResult.INVALID:
        raise HTTPException(status_code=400, detail="Invalid URL")

    # 2. Stream content
    try:
        req = client.build_request("GET", url)
        r = await client.send(req, stream=True)
        r.raise_for_status()
        
        return StreamingResponse(
            r.aiter_bytes(),
            status_code=r.status_code,
            media_type=r.headers.get("content-type"),
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=3600"
            }
        )
    except Exception as e:
        log_error(request, f"Proxy error: {str(e)}")
        raise HTTPException(status_code=502, detail="Proxy failed")