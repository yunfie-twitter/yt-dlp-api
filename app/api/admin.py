from fastapi import APIRouter, Depends, Security
from fastapi.security import APIKeyHeader
from fastapi import HTTPException
from app.config.settings import config
import os

router = APIRouter()

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: str = Security(api_key_header)):
    """Verify API key for admin endpoints"""
    expected_key = os.getenv("ADMIN_API_KEY")
    if not expected_key:
        return None
    
    if api_key != expected_key:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return api_key

@router.get("/config", dependencies=[Depends(verify_api_key)])
async def get_config():
    """Get current configuration (admin only)"""
    return {
        "rate_limit": config.rate_limit.dict(),
        "download": config.download.dict(),
        "security": config.security.dict(),
        "ytdlp": config.ytdlp.dict(),
        "i18n": config.i18n.dict()
    }