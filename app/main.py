import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from app.api import health, info, download, stream, admin
from app.config.settings import config, CONFIG_PATH

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
app.include_router(stream.router, tags=["Stream"])
app.include_router(admin.router, tags=["Admin"])

# 4. Static Files & Web Client
os.makedirs("app/static", exist_ok=True)
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

@app.on_event("startup")
async def startup_event():
    print(f"Loaded config from {CONFIG_PATH}")
    # Removed incorrect API Key check to prevent startup error
