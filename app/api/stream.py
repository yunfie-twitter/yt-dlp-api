from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import StreamingResponse
from models.request import VideoRequest
from services.stream import StreamService
from core.security import SecurityValidator, UrlValidationResult
from core.logging import log_info, log_error
from infra.rate_limit import rate_limiter
from infra.concurrency import concurrency_limiter, release_download_slot
from utils.locale import get_locale, safe_url_for_log
from i18n import i18n
import functools

router = APIRouter()

@router.post("/stream", dependencies=[Depends(rate_limiter), Depends(concurrency_limiter)])
async def stream_video(request: Request, video_request: VideoRequest):
    """Stream video download"""
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)
    
    # SSRF check
    validation_result = await SecurityValidator.validate_url(str(video_request.url))
    
    if validation_result == UrlValidationResult.BLOCKED:
        await release_download_slot(request)
        raise HTTPException(status_code=403, detail=_("error.private_ip"))
    
    if validation_result == UrlValidationResult.INVALID:
        await release_download_slot(request)
        raise HTTPException(status_code=400, detail=_("error.invalid_url", reason="Invalid format"))
    
    intent = video_request.to_intent()
    
    safe_url = safe_url_for_log(intent.url)
    log_info(request, _("log.starting_stream", url=safe_url, format="stream"))
    
    try:
        generator, headers, content_length = await StreamService.stream(intent, locale)
        
        # Wrap generator to ensure cleanup
        async def wrapped_generator():
            try:
                async for chunk in generator:
                    yield chunk
            finally:
                await release_download_slot(request)
        
        return StreamingResponse(
            wrapped_generator(),
            media_type=headers.get('Content-Type', 'application/octet-stream'),
            headers=headers
        )
        
    except HTTPException:
        await release_download_slot(request)
        raise
    except Exception as e:
        await release_download_slot(request)
        log_error(request, f"Stream error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
