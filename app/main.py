from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import health, info, download, search
from app.config.settings import config, CONFIG_PATH
from app.infra.redis import init_redis, close_redis

# 1. Create App
app = FastAPI(
    title="yt-dlp-api",
    description="High-performance API wrapper for yt-dlp",
    version="2.0.0"
)

# 2. CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Mount Routers
app.include_router(health.router, tags=["Health"])
app.include_router(info.router, tags=["Info"])
app.include_router(download.router, tags=["Download"])
app.include_router(search.router, tags=["Search"])

@app.on_event("startup")
async def startup_event():
    print(f"Loaded config from {CONFIG_PATH}")
    await init_redis()

@app.on_event("shutdown")
async def shutdown_event():
    await close_redis()
