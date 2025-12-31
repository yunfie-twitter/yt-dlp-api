import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import health, info, download, stream, admin
from app.config.settings import config, CONFIG_PATH
from app.core.state import state
from app.infra.redis import init_redis, close_redis

app = FastAPI(
    title=config.api.title,
    version=config.api.version,
    docs_url="/docs" if config.api.debug else None,
    redoc_url=None
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.api.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(health.router, tags=["Health"])
app.include_router(info.router, tags=["Info"])
app.include_router(download.router, tags=["Download"])
app.include_router(stream.router, tags=["Stream"])
app.include_router(admin.router, prefix="/admin", tags=["Admin"])

@app.on_event("startup")
async def startup_event():
    # Ensure config directory exists and file is created if missing
    config_dir = os.path.dirname(CONFIG_PATH)
    if config_dir and not os.path.exists(config_dir):
        os.makedirs(config_dir, exist_ok=True)
    
    # Reload config to ensure file creation if it didn't exist
    if not os.path.exists(CONFIG_PATH):
        config.save_to_file(CONFIG_PATH)

    state.redis = await init_redis()

@app.on_event("shutdown")
async def shutdown_event():
    await close_redis()