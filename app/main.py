import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from app.api import health, info, download, admin, search
from app.config.settings import config, CONFIG_PATH
from app.infra.redis import init_redis, close_redis
from app.infra.database import init_db

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
app.include_router(admin.router, tags=["Admin"])

# 4. Static Files & Web Client
os.makedirs("app/static", exist_ok=True)
os.makedirs("app/static/admin", exist_ok=True)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

# 5. Mount web clients directory
web_clients_path = Path("web/clients")
if web_clients_path.exists():
    app.mount("/web/clients", StaticFiles(directory="web/clients", html=True), name="web_clients")
    print(f"Mounted /web/clients from {web_clients_path.absolute()}")

# /web 以下で app/static を公開
app.mount(
    "/web",
    StaticFiles(directory="app/static", html=True),
    name="web"
)

# Admin UI Shortcut
@app.get("/admin/ui")
async def admin_ui_redirect():
    return RedirectResponse(url="/static/admin/index.html")

@app.on_event("startup")
async def startup_event():
    print(f"Loaded config from {CONFIG_PATH}")
    init_db()
    await init_redis()

@app.on_event("shutdown")
async def shutdown_event():
    await close_redis()
