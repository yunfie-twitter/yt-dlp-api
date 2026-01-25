from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from app.api import health, info, download, search
from app.config.settings import config, CONFIG_PATH
from app.infra.redis import init_redis, close_redis
from app.core.auth import get_api_key, create_api_key, verify_issuance_permission
from pydantic import BaseModel
import logging

# 1. Create App
app = FastAPI(
    title=config.api.title,
    description=config.api.description,
    version=config.api.version
)

# 2. CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.api.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Mount Routers
# Public endpoints
app.include_router(health.router, tags=["Health"])

# Protected API v1 endpoints
app.include_router(
    info.router, 
    prefix="/api/v1", 
    tags=["Info"],
    dependencies=[Depends(get_api_key)]
)
app.include_router(
    download.router, 
    prefix="/api/v1", 
    tags=["Download"],
    dependencies=[Depends(get_api_key)]
)
app.include_router(
    search.router, 
    prefix="/api/v1", 
    tags=["Search"],
    dependencies=[Depends(get_api_key)]
)

# Auth Router
class CreateKeyRequest(BaseModel):
    description: str = None

@app.post("/auth/token", tags=["Auth"], dependencies=[Depends(verify_issuance_permission)])
async def issue_api_key(request: Request, body: CreateKeyRequest):
    origin = request.headers.get("origin", "unknown")
    key_data = await create_api_key(origin, body.description)
    return key_data

# Metrics
Instrumentator().instrument(app).expose(app)

@app.on_event("startup")
async def startup_event():
    logging.basicConfig(level=config.logging.level)
    logging.info(f"Loaded config from {CONFIG_PATH}")
    
    await init_redis()
    
    # Security Warnings
    if not config.auth.api_key_enabled:
        logging.warning("⚠️  [SECURITY WARNING] API Key Authentication is DISABLED. Anyone can access the API.")
    
    if "*" in config.auth.issue_allowed_origins:
        logging.warning("⚠️  [SECURITY WARNING] API Key Issuance is open to ALL origins ('*'). This is insecure for production.")

@app.on_event("shutdown")
async def shutdown_event():
    await close_redis()
