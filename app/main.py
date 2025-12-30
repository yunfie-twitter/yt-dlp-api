from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from datetime import datetime
import uuid
import asyncio
from rich.console import Console
from rich import box
from rich.panel import Panel
from rich.table import Table

from config.settings import config
from core.state import state
from infra.redis import init_redis, close_redis
from api import health, info, stream, admin

console = Console()

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
    """Startup event with rich formatting"""
    
    # Header
    console.rule(f"[bold blue]{config.api.title} v{config.api.version}[/bold blue]")
    
    # Configuration table
    config_table = Table(title="Configuration", box=box.ROUNDED, show_header=False)
    config_table.add_column("Setting", style="cyan")
    config_table.add_column("Value", style="green")
    
    config_table.add_row("Redis URL", config.redis.url)
    config_table.add_row("Rate Limit", f"{config.rate_limit.max_requests} req/{config.rate_limit.window_seconds}s")
    config_table.add_row("Max Concurrent", str(config.download.max_concurrent))
    config_table.add_row("Download Timeout", f"{config.download.timeout_seconds}s")
    config_table.add_row("SSRF Protection", "✓" if config.security.enable_ssrf_protection else "✗")
    config_table.add_row("Default Locale", config.i18n.default_locale)
    config_table.add_row("Log Level", config.logging.level)
    
    console.print(config_table)
    console.print()
    
    # Initialize components
    console.print("[bold cyan]Initializing components...[/bold cyan]")
    
    # Redis
    await init_redis()
    
    # yt-dlp version check
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--version',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        state.ytdlp_version = stdout.decode().strip()
        console.print(f"[green]✓ yt-dlp {state.ytdlp_version}[/green]")
    except Exception as e:
        console.print(f"[red]✗ yt-dlp version check failed: {str(e)}[/red]")
    
    # Deno check
    try:
        process = await asyncio.create_subprocess_exec(
            'deno', '--version',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        version_output = stdout.decode().strip()
        
        for line in version_output.split('\n'):
            if line.startswith('deno '):
                state.deno_version = line.split()[1]
                console.print(f"[green]✓ Deno {state.deno_version}[/green]")
                break
    except Exception:
        console.print(f"[yellow]⚠ Deno not found (YouTube downloads may fail)[/yellow]")
    
    # JS Runtime detection
    if config.ytdlp.js_runtime:
        state.js_runtime = config.ytdlp.js_runtime
        console.print(f"[cyan]✓ JS Runtime: {state.js_runtime} (configured)[/cyan]")
    elif config.ytdlp.auto_detect_runtime:
        import os
        runtimes = [
            ("deno", "/usr/local/bin/deno"),
            ("deno", "/usr/bin/deno"),
            ("node", "/usr/local/bin/node"),
            ("node", "/usr/bin/node"),
        ]
        
        for runtime_name, runtime_path in runtimes:
            if os.path.exists(runtime_path):
                try:
                    process = await asyncio.create_subprocess_exec(
                        runtime_path, '--version',
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await asyncio.wait_for(process.communicate(), timeout=3.0)
                    if process.returncode == 0:
                        state.js_runtime = f"{runtime_name}:{runtime_path}"
                        console.print(f"[green]✓ JS Runtime: {runtime_name} (auto-detected)[/green]")
                        break
                except:
                    pass
        
        if not state.js_runtime:
            console.print(f"[yellow]⚠ No JS runtime detected[/yellow]")
    
    # Load supported sites (background task)
    asyncio.create_task(load_supported_sites())
    
    # Success banner
    console.print()
    success_panel = Panel(
        "[bold green]✅ API Ready[/bold green]\n"
        f"Listening on http://0.0.0.0:8000\n"
        f"Docs: http://0.0.0.0:8000/docs",
        title="[bold green]Status[/bold green]",
        border_style="green",
        box=box.DOUBLE
    )
    console.print(success_panel)
    console.rule(style="green")

async def load_supported_sites():
    """Load supported sites with rich output"""
    import json
    import hashlib
    
    redis = state.redis
    
    # Try Redis cache first
    if redis:
        try:
            cached = await redis.get("supported_sites:cache")
            if cached:
                sites_list = json.loads(cached)
                state.supported_sites = tuple(sites_list)
                state.supported_sites_etag = hashlib.sha256(cached.encode()).hexdigest()[:16]
                console.print(f"[green]✓ Loaded {len(state.supported_sites)} supported sites from cache[/green]")
                return
        except Exception as e:
            console.print(f"[yellow]⚠ Failed to load sites from cache: {str(e)}[/yellow]")
    
    # Load from yt-dlp
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--list-extractors',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15.0)
        sites_list = stdout.decode().strip().split('\n')
        state.supported_sites = tuple(sites_list)
        
        sites_json = json.dumps(sites_list)
        state.supported_sites_etag = hashlib.sha256(sites_json.encode()).hexdigest()[:16]
        
        # Cache in Redis
        if redis:
            try:
                await redis.setex("supported_sites:cache", 86400, sites_json)
            except Exception as e:
                console.print(f"[yellow]⚠ Failed to cache sites: {str(e)}[/yellow]")
        
        console.print(f"[green]✓ Loaded {len(state.supported_sites)} supported sites[/green]")
    except Exception as e:
        console.print(f"[red]✗ Failed to load supported sites: {str(e)}[/red]")

# Shutdown
@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown event with rich formatting"""
    console.rule("[bold yellow]Shutting Down[/bold yellow]")
    await close_redis()
    console.print("[dim]✓ Cleanup complete[/dim]")
    console.rule(style="dim")

# Exception handlers
@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """General exception handler"""
    console.print(f"[red]✗ Unhandled exception: {str(exc)}[/red]")
    
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "detail": "Internal server error" if config.logging.level != "DEBUG" else str(exc),
            "request_id": getattr(request.state, "request_id", "unknown"),
            "timestamp": datetime.now().isoformat()
        }
    )
