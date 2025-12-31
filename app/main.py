from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import health, info, download, stream, admin
from app.config.settings import config
from app.core.state import state
from app.infra.redis import init_redis, close_redis

app = FastAPI(
    title="yt-dlp API",
    version="1.0.0",
    docs_url="/docs" if config.app.debug else None,
    redoc_url=None
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    state.redis = await init_redis()

@app.on_event("shutdown")
async def shutdown_event():
    await close_redis()