from fastapi import APIRouter

from app.core.state import state
from app.i18n import i18n
from app.infra.redis import get_redis  # noqa: F401

router = APIRouter()


@router.get("/")
async def root():
    """Root endpoint"""
    return {
        "status": i18n.get("response.status_running"),
        "service": "yt-dlp Streaming API",
        "version": "4.0.0",
        "deno_version": state.deno_version,
        "ytdlp_version": state.ytdlp_version,
        "redis_enabled": state.redis is not None
    }


@router.get("/health")
async def health_check():
    """Lightweight health check"""
    redis_status = i18n.get("response.redis_disabled")
    if state.redis:
        try:
            await state.redis.ping()
            redis_status = i18n.get("response.redis_connected")
        except Exception:
            redis_status = i18n.get("response.redis_disconnected")

    return {
        "status": i18n.get("health.status"),
        "redis": redis_status
    }


@router.get("/health/full")
async def health_check_full():
    """Detailed health check"""
    redis_status = i18n.get("response.redis_disabled")
    active_downloads = 0

    if state.redis:
        try:
            await state.redis.ping()
            redis_status = i18n.get("response.redis_connected")
            active_downloads = int(await state.redis.get("active_downloads_count") or 0)
        except Exception:
            redis_status = i18n.get("response.redis_disconnected")

    return {
        "status": i18n.get("health.status"),
        "ytdlp_version": state.ytdlp_version,
        "deno_version": state.deno_version,
        "redis_status": redis_status,
        "js_runtime": state.js_runtime,
        "active_downloads": active_downloads,
        "supported_sites_loaded": len(state.supported_sites)
    }
