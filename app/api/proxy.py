"""
Proxy list endpoint.
Only active when proxy.enabled and proxy.expose_list_endpoint are both true in config.
"""

from fastapi import APIRouter, HTTPException

from app.config.settings import config
from app.services.proxy import ProxyService

router = APIRouter()


@router.get("/proxies")
async def list_proxies():
    """
    List configured proxy URLs with their index.
    Returns 404 if proxy is disabled or endpoint is not exposed.
    """
    if not config.proxy.enabled or not config.proxy.expose_list_endpoint:
        raise HTTPException(status_code=404, detail="Not Found")

    return {
        "enabled": True,
        "count": ProxyService.get_count(),
        "proxies": ProxyService.get_list(),
    }
