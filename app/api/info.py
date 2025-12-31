from fastapi import APIRouter, Request, Depends, HTTPException
from app.models.request import VideoRequest
from app.models.response import VideoInfo
from app.services.info import VideoInfoService
from app.core.security import SecurityValidator, UrlValidationResult
from app.core.logging import log_info, log_error
from app.infra.rate_limit import rate_limiter
from app.utils.locale import get_locale, safe_url_for_log
from app.i18n import i18n
import functools

router = APIRouter()

@router.post("/info", response_model=VideoInfo, dependencies=[Depends(rate_limiter)])
async def get_video_info(request: Request, video_request: VideoRequest):
    """Get video information with caching"""
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)
    
    # SSRF check (separated from validation layer)
    validation_result = await SecurityValidator.validate_url(str(video_request.url))
    
    if validation_result == UrlValidationResult.BLOCKED:
        raise HTTPException(status_code=403, detail=_("error.private_ip"))
    
    if validation_result == UrlValidationResult.INVALID:
        raise HTTPException(status_code=400, detail=_("error.invalid_url", reason="Invalid format"))
    
    safe_url = safe_url_for_log(str(video_request.url))
    log_info(request, _("log.fetching_info", url=safe_url))
    
    try:
        video_info = await VideoInfoService.fetch(video_request, locale)
        log_info(request, _("log.info_retrieved", title=video_info.title))
        return video_info
    except HTTPException:
        raise
    except Exception as e:
        log_error(request, f"Video info error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))