from fastapi import APIRouter, Depends, HTTPException, Body
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.services.auth_service import auth_service
from app.infra.database import get_db
from app.config.settings import config
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/admin")
security = HTTPBearer()

class LoginRequest(BaseModel):
    password: str

class ConfigUpdateRequest(BaseModel):
    rate_limit: Optional[dict] = None
    download: Optional[dict] = None
    security: Optional[dict] = None
    ytdlp: Optional[dict] = None

@router.post("/login")
async def login(request: LoginRequest, db: Session = Depends(get_db)):
    if config.auth.sso_enabled:
        raise HTTPException(400, "Password login is disabled when SSO is enabled")
    
    if request.password != config.auth.admin_password:
        raise HTTPException(401, "Invalid password")
    
    token = auth_service.create_access_token({"sub": "admin"})
    return {"access_token": token, "token_type": "bearer"}

@router.get("/config")
async def get_config(credentials: HTTPAuthorizationCredentials = Depends(security)):
    # Simple token verification (should be robust in prod)
    if not credentials:
        raise HTTPException(401, "Invalid token")
        
    return {
        "rate_limit": config.rate_limit.model_dump(),
        "download": config.download.model_dump(),
        "security": config.security.model_dump(),
        "ytdlp": config.ytdlp.model_dump()
    }

@router.put("/config")
async def update_config(
    update: ConfigUpdateRequest,
    credentials: HTTPAuthorizationCredentials = Depends(security)
):
    if update.rate_limit:
        config.rate_limit = config.rate_limit.model_validate(update.rate_limit)
    if update.download:
        config.download = config.download.model_validate(update.download)
    if update.security:
        config.security = config.security.model_validate(update.security)
        
    # Save to disk
    from app.config.settings import CONFIG_PATH
    config.save_to_file(CONFIG_PATH)
    
    return {"status": "success"}

@router.get("/login/google")
async def google_login():
    # Placeholder for actual OAuth redirect
    return {"url": "https://accounts.google.com/o/oauth2/auth..."}
