from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from datetime import datetime
import uuid
import asyncio

from config.settings import config
from core.state import state
from infra.redis import init_redis, close_redis
from api import health, info, stream, admin

# Initialize FastAPI
app = FastAPI(
    title=config.api.title,
    description=config.api.description,
    version=config.api.version
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.api.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request ID middleware
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response

# Register routers
app.include_router(health.router, tags=["Health"])
app.include_router(info.router, tags=["Video Info"])
app.include_router(stream.router, tags=["Download"])
app.include_router(admin.router, tags=["Admin"])

# Startup
@app.on_event("startup")
async def startup_event():
    print(f"🚀 Starting {config.api.title} v{config.api.version}")
    
    await init_redis()
    
    # Initialize yt-dlp version
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--version',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        state.ytdlp_version = stdout.decode().strip()
        print(f"yt-dlp {state.ytdlp_version}")
    except:
        print("yt-dlp version check failed")
    
    print(f"API Ready")

# Shutdown
@app.on_event("shutdown")
async def shutdown_event():
    print("Shutting down")
    await close_redis()

# Exception handlers
@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "detail": "Internal server error",
            "request_id": request.state.request_id,
            "timestamp": datetime.now().isoformat()
        }
    )
