import asyncio
import logging
import shutil

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from app.api import download, health, info, proxy, search
from app.config.settings import CONFIG_PATH, config
from app.core.auth import create_api_key, get_api_key, verify_issuance_permission
from app.core.state import state
from app.infra.redis import close_redis, init_redis

# 1. Create App
app = FastAPI(title=config.api.title, description=config.api.description, version=config.api.version)

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
app.include_router(info.router, prefix="/api/v1", tags=["Info"], dependencies=[Depends(get_api_key)])
app.include_router(download.router, prefix="/api/v1", tags=["Download"], dependencies=[Depends(get_api_key)])
app.include_router(search.router, prefix="/api/v1", tags=["Search"], dependencies=[Depends(get_api_key)])
app.include_router(proxy.router, prefix="/api/v1", tags=["Proxy"], dependencies=[Depends(get_api_key)])


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


async def _detect_version(cmd: str, args: list[str]) -> str:
    """Run a command and return stripped stdout, or 'not installed'."""
    if not shutil.which(cmd):
        return "not installed"
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        return stdout.decode().strip() or "unknown"
    except Exception:
        return "unknown"


async def _detect_runtime() -> None:
    """Detect yt-dlp version, JS runtime, and Deno version at startup."""
    # yt-dlp version
    state.ytdlp_version = await _detect_version("yt-dlp", ["--version"])
    logging.info(f"yt-dlp version: {state.ytdlp_version}")

    # JS runtime detection
    if config.ytdlp.js_runtime:
        # Explicit config takes priority
        state.js_runtime = config.ytdlp.js_runtime
        logging.info(f"JS runtime (configured): {state.js_runtime}")
    elif config.ytdlp.auto_detect_runtime:
        # Auto-detect: try deno first, then node
        for runtime in ["deno", "node"]:
            path = shutil.which(runtime)
            if path:
                state.js_runtime = f"{runtime}:{path}"
                logging.info(f"JS runtime (auto-detected): {state.js_runtime}")
                break
        else:
            state.js_runtime = None
            logging.warning("No JS runtime found (deno/node not installed)")

    # Deno version
    if state.js_runtime and "deno" in state.js_runtime:
        state.deno_version = await _detect_version("deno", ["--version"])
        # deno --version outputs multiple lines; take just the first
        state.deno_version = state.deno_version.split("\n")[0]
    elif shutil.which("deno"):
        state.deno_version = await _detect_version("deno", ["--version"])
        state.deno_version = state.deno_version.split("\n")[0]
    else:
        state.deno_version = "not installed"

    logging.info(f"Deno version: {state.deno_version}")


@app.on_event("startup")
async def startup_event():
    logging.basicConfig(level=config.logging.level)
    logging.info(f"Loaded config from {CONFIG_PATH}")

    await init_redis()
    await _detect_runtime()

    # Start worker pool
    from app.services.worker_pool import WorkerPool

    pool = WorkerPool(pool_size=config.download.worker_pool_size)
    pool.start()
    state.worker_pool = pool

    # Security Warnings
    if not config.auth.api_key_enabled:
        logging.warning("⚠️  [SECURITY WARNING] API Key Authentication is DISABLED. Anyone can access the API.")

    if "*" in config.auth.issue_allowed_origins:
        logging.warning(
            "⚠️  [SECURITY WARNING] API Key Issuance is open to ALL origins ('*'). This is insecure for production."
        )

    # Proxy info
    if config.proxy.enabled:
        logging.info(f"Proxy enabled with {len(config.proxy.urls)} URL(s)")
    else:
        logging.info("Proxy disabled")


@app.on_event("shutdown")
async def shutdown_event():
    if state.worker_pool is not None:
        state.worker_pool.stop()
        state.worker_pool = None
    await close_redis()
