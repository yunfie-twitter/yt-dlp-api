from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl, Field, validator
from pydantic_settings import BaseSettings
import subprocess
import asyncio
from typing import Optional, List, Dict
import unicodedata
import re
from datetime import datetime
import json
import os
import ipaddress
import socket
from urllib.parse import urlparse

# Rich logging
from rich.logging import RichHandler
from rich.console import Console
import logging

# i18n
from i18n import i18n

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True)]
)
logger = logging.getLogger(__name__)

# Settings
class Settings(BaseSettings):
    redis_url: str = "redis://redis:6379"
    rate_limit_requests: int = 5
    rate_limit_window: int = 60
    max_concurrent_downloads: int = 10
    max_download_timeout: int = 3600
    log_level: str = "INFO"
    yt_dlp_js_runtime: Optional[str] = None
    enable_ssrf_protection: bool = True
    default_locale: str = "en"
    
    class Config:
        env_file = ".env"

settings = Settings()
logging.getLogger().setLevel(getattr(logging, settings.log_level))

# Update i18n default locale
i18n.default_locale = settings.default_locale

app = FastAPI(
    title=i18n.get("api.title"),
    description=i18n.get("api.description"),
    version=i18n.get("api.version")
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis
try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning(i18n.get("startup.redis_not_installed"))

SUPPORTED_SITES: List[str] = []
redis_client: Optional[aioredis.Redis] = None
DENO_VERSION: str = "unknown"
YTDLP_VERSION: str = "unknown"
JS_RUNTIME_PATH: Optional[str] = None

def get_locale(accept_language: Optional[str] = None) -> str:
    """Extract locale from Accept-Language header"""
    if not accept_language:
        return settings.default_locale
    
    # Parse Accept-Language header (e.g., "en-US,en;q=0.9,ja;q=0.8")
    languages = []
    for lang in accept_language.split(","):
        parts = lang.strip().split(";")
        locale = parts[0].split("-")[0]  # Extract main language code
        languages.append(locale)
    
    # Return first supported locale
    for locale in languages:
        if locale in i18n.translations:
            return locale
    
    return settings.default_locale

class VideoRequest(BaseModel):
    url: HttpUrl = Field(..., description="Video URL")
    format: Optional[str] = Field(None, description="Custom format specification")
    audio_only: Optional[bool] = Field(False, description="Download audio only")
    audio_format: Optional[str] = Field("opus", description="Audio format (opus/m4a/mp3)")
    quality: Optional[int] = Field(None, description="Video quality", ge=144, le=2160)
    
    @validator('url')
    def validate_url_safety(cls, v):
        """SSRF protection: prevent access to internal IPs"""
        if not settings.enable_ssrf_protection:
            return v
        
        try:
            parsed = urlparse(str(v))
            hostname = parsed.hostname
            
            if not hostname:
                raise ValueError("Invalid URL")
            
            # Check if IP address
            try:
                ip = ipaddress.ip_address(hostname)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    raise ValueError(i18n.get("error.private_ip"))
            except ValueError:
                # Hostname - resolve and check
                try:
                    resolved_ip = socket.gethostbyname(hostname)
                    ip = ipaddress.ip_address(resolved_ip)
                    if ip.is_private or ip.is_loopback or ip.is_link_local:
                        raise ValueError(i18n.get("error.ip_resolves_private"))
                except socket.gaierror:
                    pass
            
            return v
        except Exception as e:
            logger.error(f"URL validation failed: {str(e)}")
            raise ValueError(i18n.get("error.invalid_url", reason=str(e)))

class VideoInfo(BaseModel):
    title: str
    duration: Optional[int]
    ext: str
    filesize: Optional[int]
    formats: List[Dict]
    thumbnail: Optional[str]
    uploader: Optional[str]
    webpage_url: str
    is_live: bool = False

def sanitize_filename(name: str, max_length: int = 200) -> str:
    """Sanitize filename for safe storage"""
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    
    windows_reserved = {
        'CON', 'PRN', 'AUX', 'NUL',
        'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
    }
    if name.upper() in windows_reserved:
        name = f"_{name}"
    
    return name[:max_length].strip()

def safe_url_for_log(url: str) -> str:
    """Safe URL for logging (remove tokens)"""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except:
        return "invalid_url"

class RedisRateLimiter:
    """Redis-based rate limiter with Lua script"""
    
    def __init__(self):
        self.max_requests = settings.rate_limit_requests
        self.window_seconds = settings.rate_limit_window
        
        self.lua_script = """
        local key = KEYS[1]
        local limit = tonumber(ARGV[1])
        local window = tonumber(ARGV[2])
        
        local current = redis.call('INCR', key)
        if current == 1 then
            redis.call('EXPIRE', key, window)
        end
        
        if current > limit then
            local ttl = redis.call('TTL', key)
            return {0, ttl}
        end
        
        return {1, 0}
        """
    
    async def __call__(self, request: Request):
        if not REDIS_AVAILABLE or redis_client is None:
            return True
        
        client_ip = request.client.host
        endpoint = request.url.path
        user_agent_hash = hash(request.headers.get("user-agent", ""))
        
        key = f"rate:{client_ip}:{endpoint}:{user_agent_hash % 10000}"
        
        try:
            result = await redis_client.eval(
                self.lua_script,
                1,
                key,
                self.max_requests,
                self.window_seconds
            )
            
            allowed, ttl = result
            
            if not allowed:
                locale = get_locale(request.headers.get("accept-language"))
                raise HTTPException(
                    status_code=429,
                    detail=i18n.get("error.rate_limit", locale=locale, seconds=ttl),
                    headers={"Retry-After": str(ttl)}
                )
            
            return True
        except HTTPException:
            raise
        except Exception as e:
            logger.error(i18n.get("log.redis_rate_limit_error", error=str(e)))
            return True

class ConcurrencyLimiter:
    """Concurrent download limiter"""
    
    async def __call__(self, request: Request):
        if not REDIS_AVAILABLE or redis_client is None:
            return True
        
        key = "active_downloads"
        
        try:
            current = await redis_client.incr(key)
            
            if current > settings.max_concurrent_downloads:
                await redis_client.decr(key)
                locale = get_locale(request.headers.get("accept-language"))
                raise HTTPException(
                    status_code=503,
                    detail=i18n.get("error.server_busy", locale=locale, max=settings.max_concurrent_downloads)
                )
            
            request.state.download_slot_acquired = True
            
            return True
        except HTTPException:
            raise
        except Exception as e:
            logger.error(i18n.get("log.concurrency_error", error=str(e)))
            return True

rate_limiter = RedisRateLimiter()
concurrency_limiter = ConcurrencyLimiter()

async def release_download_slot(request: Request):
    """Release download slot"""
    if hasattr(request.state, 'download_slot_acquired') and request.state.download_slot_acquired:
        if redis_client:
            try:
                await redis_client.decr("active_downloads")
            except Exception as e:
                logger.error(i18n.get("log.slot_release_failed", error=str(e)))

async def detect_js_runtime() -> Optional[str]:
    """Auto-detect JS runtime"""
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
                    logger.info(i18n.get("log.js_runtime_detected", name=runtime_name, path=runtime_path))
                    return f"{runtime_name}:{runtime_path}"
            except:
                pass
    
    return None

async def check_deno_installation() -> Dict[str, str]:
    """Check Deno installation status"""
    try:
        process = await asyncio.create_subprocess_exec(
            'deno', '--version',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        version_output = stdout.decode().strip()
        
        deno_version = "unknown"
        for line in version_output.split('\n'):
            if line.startswith('deno '):
                deno_version = line.split()[1]
                break
        
        return {
            "status": i18n.get("response.deno_installed"),
            "version": deno_version,
            "path": "/usr/local/bin/deno"
        }
    except Exception as e:
        return {
            "status": i18n.get("response.deno_not_found"),
            "error": str(e)
        }

async def load_supported_sites():
    """Load supported sites in background"""
    global SUPPORTED_SITES
    
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--list-extractors',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15.0)
        SUPPORTED_SITES = stdout.decode().strip().split('\n')
        console.print(f"[green]{i18n.get('startup.sites_cached', count=len(SUPPORTED_SITES))}[/green]")
        logger.info(i18n.get("startup.sites_cached", count=len(SUPPORTED_SITES)))
    except Exception as e:
        console.print(f"[yellow]{i18n.get('startup.sites_failed')}[/yellow]")
        logger.warning(i18n.get("log.extractor_load_failed", error=str(e)))

@app.on_event("startup")
async def startup_event():
    """Startup handler"""
    global redis_client, DENO_VERSION, YTDLP_VERSION, JS_RUNTIME_PATH
    
    console.rule(f"[bold blue]{i18n.get('startup.api_ready')}[/bold blue]")
    
    # JS runtime detection
    if settings.yt_dlp_js_runtime:
        JS_RUNTIME_PATH = settings.yt_dlp_js_runtime
        console.print(f"[cyan]{i18n.get('startup.js_runtime_configured', runtime=JS_RUNTIME_PATH)}[/cyan]")
    else:
        JS_RUNTIME_PATH = await detect_js_runtime()
        if JS_RUNTIME_PATH:
            console.print(f"[green]{i18n.get('startup.js_runtime_detected', runtime=JS_RUNTIME_PATH)}[/green]")
        else:
            console.print(f"[yellow]{i18n.get('startup.js_runtime_none')}[/yellow]")
    
    # Deno check
    deno_info = await check_deno_installation()
    if deno_info["status"] == i18n.get("response.deno_installed"):
        DENO_VERSION = deno_info["version"]
        console.print(f"[green]{i18n.get('startup.deno_detected', version=DENO_VERSION)}[/green]")
    
    # yt-dlp check
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--version',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        YTDLP_VERSION = stdout.decode().strip()
        console.print(f"[green]{i18n.get('startup.ytdlp_detected', version=YTDLP_VERSION)}[/green]")
    except Exception as e:
        console.print(f"[red]{i18n.get('startup.ytdlp_failed')}[/red]")
        logger.error(i18n.get("startup.ytdlp_failed"))
    
    # Redis connection
    if REDIS_AVAILABLE:
        try:
            redis_client = await aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5
            )
            await redis_client.ping()
            console.print(f"[green]{i18n.get('startup.redis_connected')}[/green]")
            logger.info(i18n.get("startup.redis_connected"))
        except Exception as e:
            console.print(f"[yellow]{i18n.get('startup.redis_failed', error=str(e))}[/yellow]")
            logger.warning(i18n.get("startup.redis_failed", error=str(e)))
            redis_client = None
    
    # Load supported sites in background
    asyncio.create_task(load_supported_sites())
    
    console.rule(f"[bold green]{i18n.get('startup.api_ready')}[/bold green]")
    console.print(f"[cyan]{i18n.get('startup.max_downloads', count=settings.max_concurrent_downloads)}[/cyan]")
    console.print(f"[cyan]{i18n.get('startup.download_timeout', seconds=settings.max_download_timeout)}[/cyan]")
    console.print(f"[cyan]{i18n.get('startup.ssrf_protection', enabled=settings.enable_ssrf_protection)}[/cyan]")

@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown handler"""
    console.rule(f"[bold yellow]{i18n.get('shutdown.closing')}[/bold yellow]")
    
    if redis_client:
        await redis_client.close()
        logger.info(i18n.get("shutdown.redis_closed"))

@app.get("/", tags=["Health"])
async def root():
    return {
        "status": i18n.get("response.status_running"),
        "service": i18n.get("api.title"),
        "version": i18n.get("api.version"),
        "deno_version": DENO_VERSION,
        "ytdlp_version": YTDLP_VERSION,
        "redis_enabled": redis_client is not None,
        "max_concurrent_downloads": settings.max_concurrent_downloads
    }

@app.get("/health", tags=["Health"])
async def health_check():
    """Lightweight health check for Kubernetes"""
    redis_status = i18n.get("response.redis_disabled")
    if redis_client:
        try:
            await redis_client.ping()
            redis_status = i18n.get("response.redis_connected")
        except:
            redis_status = i18n.get("response.redis_disconnected")
    
    return {
        "status": i18n.get("health.status"),
        "redis": redis_status
    }

@app.get("/health/full", tags=["Health"])
async def health_check_full():
    """Detailed health check"""
    
    # yt-dlp check
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--version',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        ytdlp_version = stdout.decode().strip()
    except Exception as e:
        raise HTTPException(status_code=503, detail=i18n.get("error.ytdlp_unavailable", reason=str(e)))
    
    # Deno check
    deno_info = await check_deno_installation()
    
    # Redis check
    redis_status = i18n.get("response.redis_disabled")
    active_downloads = 0
    if redis_client:
        try:
            await redis_client.ping()
            redis_status = i18n.get("response.redis_connected")
            active_downloads = int(await redis_client.get("active_downloads") or 0)
        except:
            redis_status = i18n.get("response.redis_disconnected")
    
    health_data = {
        "status": i18n.get("health.status"),
        "ytdlp_version": ytdlp_version,
        "deno": deno_info,
        "redis_status": redis_status,
        "js_runtime": JS_RUNTIME_PATH,
        "active_downloads": active_downloads,
        "max_downloads": settings.max_concurrent_downloads,
        "supported_sites_loaded": len(SUPPORTED_SITES)
    }
    
    if deno_info["status"] != i18n.get("response.deno_installed"):
        health_data["warning"] = i18n.get("health.warning_no_deno")
    
    return health_data

@app.post("/info", response_model=VideoInfo, tags=["Video Info"], dependencies=[Depends(rate_limiter)])
async def get_video_info(request: Request, video_request: VideoRequest):
    """Get video information"""
    
    locale = get_locale(request.headers.get("accept-language"))
    
    cmd = [
        'yt-dlp',
        '--dump-json',
        '--no-playlist',
        '--match-filter', '!is_live',
        '--socket-timeout', '10',
        '--retries', '3',
    ]
    
    if JS_RUNTIME_PATH:
        cmd.extend(['--js-runtimes', JS_RUNTIME_PATH])
    
    cmd.append(str(video_request.url))
    
    safe_url = safe_url_for_log(str(video_request.url))
    logger.info(i18n.get("log.fetching_info", url=safe_url))
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.error(i18n.get("log.info_timeout"))
            raise HTTPException(status_code=504, detail=i18n.get("error.timeout", locale=locale))
        
        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error(i18n.get("log.ytdlp_error", code=process.returncode, message=error_msg))
            raise HTTPException(status_code=400, detail=i18n.get("error.fetch_info_failed", locale=locale, reason=error_msg[:200]))
        
        info = json.loads(stdout.decode())
        
        is_live = info.get('is_live', False)
        if is_live:
            raise HTTPException(status_code=400, detail=i18n.get("error.live_not_supported", locale=locale))
        
        logger.info(i18n.get("log.info_retrieved", title=info.get('title', 'Unknown')))
        
        return VideoInfo(
            title=info.get("title", "Unknown"),
            duration=info.get("duration"),
            ext=info.get("ext", "mp4"),
            filesize=info.get("filesize"),
            formats=[
                {
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "resolution": f.get("resolution"),
                    "filesize": f.get("filesize"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                }
                for f in info.get("formats", [])[:20]
            ],
            thumbnail=info.get("thumbnail"),
            uploader=info.get("uploader"),
            webpage_url=info.get("webpage_url", str(video_request.url)),
            is_live=is_live
        )
        
    except json.JSONDecodeError as e:
        logger.error(i18n.get("log.json_parse_error", error=str(e)))
        raise HTTPException(status_code=500, detail=i18n.get("error.parse_failed", locale=locale))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Video info error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/stream", tags=["Download"], dependencies=[Depends(rate_limiter), Depends(concurrency_limiter)])
async def stream_video(request: Request, video_request: VideoRequest):
    """Stream video download"""
    
    locale = get_locale(request.headers.get("accept-language"))
    
    # Format selection
    if video_request.format:
        format_str = video_request.format
        ext = 'mp4'
        media_type = 'application/octet-stream'
    elif video_request.audio_only:
        if video_request.audio_format == "mp3":
            format_str = 'bestaudio'
            ext = 'mp3'
            media_type = 'audio/mpeg'
        elif video_request.audio_format == "m4a":
            format_str = 'bestaudio[ext=m4a]/bestaudio'
            ext = 'm4a'
            media_type = 'audio/mp4'
        else:
            format_str = 'bestaudio[ext=webm]/bestaudio'
            ext = 'webm'
            media_type = 'audio/webm'
    elif video_request.quality:
        height = video_request.quality
        format_str = (
            f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]/"
            f"best[ext=mp4][height<={height}]/best"
        )
        ext = 'mp4'
        media_type = 'application/octet-stream'
    else:
        format_str = 'best'
        ext = 'mp4'
        media_type = 'application/octet-stream'
    
    # Get filename with --print
    filename = f"video.{ext}"
    
    try:
        filename_cmd = [
            'yt-dlp',
            '--print', 'filename',
            '-o', '%(title)s.%(ext)s',
            '--no-playlist',
        ]
        
        if JS_RUNTIME_PATH:
            filename_cmd.extend(['--js-runtimes', JS_RUNTIME_PATH])
        
        filename_cmd.append(str(video_request.url))
        
        filename_process = await asyncio.create_subprocess_exec(
            *filename_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        stdout, _ = await asyncio.wait_for(filename_process.communicate(), timeout=10.0)
        
        if filename_process.returncode == 0:
            raw_filename = stdout.decode().strip()
            title = os.path.splitext(raw_filename)[0]
            filename = f"{sanitize_filename(title)}.{ext}"
    except Exception as e:
        logger.warning(i18n.get("log.filename_failed", error=str(e)))
    
    cmd = [
        'yt-dlp',
        str(video_request.url),
        '-f', format_str,
        '-o', '-',
        '--no-playlist',
        '--match-filter', '!is_live',
        '--socket-timeout', '10',
        '--retries', '3',
    ]
    
    if JS_RUNTIME_PATH:
        cmd.extend(['--js-runtimes', JS_RUNTIME_PATH])
    
    if settings.log_level != "DEBUG":
        cmd.extend(['--quiet', '--no-warnings'])
    
    if video_request.audio_only and video_request.audio_format == "mp3":
        cmd.extend(['-x', '--audio-format', 'mp3'])
    
    safe_url = safe_url_for_log(str(video_request.url))
    logger.info(i18n.get("log.starting_stream", url=safe_url, format=format_str))
    
    process = None
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        stderr_lines = []
        
        async def drain_stderr():
            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    stderr_lines.append(decoded)
                    if settings.log_level == "DEBUG":
                        logger.debug(f"yt-dlp: {decoded}")
            except:
                pass
        
        stderr_task = asyncio.create_task(drain_stderr())
        
        async def generate():
            CHUNK_SIZE = 1024 * 1024
            try:
                while True:
                    chunk = await process.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            except asyncio.CancelledError:
                logger.warning(i18n.get("log.client_disconnected", pid=process.pid))
                process.kill()
                await process.wait()
                raise
            except Exception as e:
                logger.error(i18n.get("log.stream_error", error=str(e)))
                process.kill()
                await process.wait()
                raise
            finally:
                try:
                    returncode = await asyncio.wait_for(process.wait(), timeout=5.0)
                    
                    if returncode != 0:
                        error_summary = '\n'.join(stderr_lines[-10:])
                        logger.error(i18n.get("log.process_exit_error", code=returncode))
                    
                except asyncio.TimeoutError:
                    logger.warning(i18n.get("log.process_timeout"))
                    process.kill()
                    await process.wait()
                
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
                
                await release_download_slot(request)
        
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'no-cache',
        }
        
        return StreamingResponse(
            generate(),
            media_type=media_type,
            headers=headers
        )
        
    except Exception as e:
        await release_download_slot(request)
        
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        
        logger.error(i18n.get("log.stream_error", error=str(e)))
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/supported-sites", tags=["Info"])
async def get_supported_sites(
    limit: int = 50,
    search: Optional[str] = None
):
    """Get supported sites list"""
    
    sites = SUPPORTED_SITES
    
    if search:
        search_lower = search.lower()
        sites = [s for s in sites if search_lower in s.lower()]
    
    return {
        "count": len(sites),
        "total": len(SUPPORTED_SITES),
        "sites": sites[:limit],
        "cache_control": "max-age=3600"
    }

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    await release_download_slot(request)
    
    safe_url = safe_url_for_log(str(request.url))
    logger.error(i18n.get("log.http_error", code=exc.status_code, detail=exc.detail, url=safe_url))
    
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": True,
            "status_code": exc.status_code,
            "detail": exc.detail,
            "timestamp": datetime.now().isoformat()
        },
        headers=exc.headers or {}
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    await release_download_slot(request)
    
    logger.exception(i18n.get("log.unhandled_exception", error=str(exc)))
    
    locale = get_locale(request.headers.get("accept-language"))
    
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "status_code": 500,
            "detail": i18n.get("error.internal", locale=locale),
            "timestamp": datetime.now().isoformat()
        }
    )
