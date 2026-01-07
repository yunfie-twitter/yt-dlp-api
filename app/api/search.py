import functools
from fastapi import APIRouter, Request, Depends, HTTPException, Query

from app.core.logging import log_info, log_error
from app.infra.rate_limit import rate_limiter
from app.utils.locale import get_locale
from app.i18n import i18n

from app.models.response import SearchResponse
from app.services.search import VideoSearchService

router = APIRouter()

@router.get("/search", response_model=SearchResponse, dependencies=[Depends(rate_limiter)])
async def search_videos(
    request: Request,
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(5, ge=1, le=20, description="Maximum results")
):
    """Search videos using yt-dlp's ytsearch."""

    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)

    log_info(request, f"Search request: q={q} limit={limit}")

    try:
        return await VideoSearchService.search(query=q, limit=limit, locale=locale)
    except HTTPException:
        raise
    except Exception as e:
        log_error(request, f"Search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
