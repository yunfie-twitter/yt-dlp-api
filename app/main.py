from fastapi import FastAPI, HTTPException, Request, Depends, Security
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, HttpUrl, Field, validator
import subprocess
import asyncio
from typing import Optional, List, Dict, Tuple
import unicodedata
import re
from datetime import datetime
import json
import os
import ipaddress
import socket
from urllib.parse import urlparse
import hashlib
import uuid
from collections import deque
import functools

# Rich logging
from rich.logging import RichHandler
from rich.console import Console
import logging

# Configuration
from config import config

# i18n
from i18n import i18n

console = Console()

# Setup logging
if config.logging.enable_rich:
    logging.basicConfig(
        level=getattr(logging, config.logging.level),
        format=config.logging.format,
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)]
    )
else:
    logging.basicConfig(
        level=getattr(logging, config.logging.level),
        format=config.logging.format
    )

logger = logging.getLogger(__name__)

# Update i18n
i18n.default_locale = config.i18n.default_locale

app = FastAPI(
    title=config.api.title,
    description=config.api.description,
    version=config.api.version,
    docs_url=config.api.docs_url,
    redoc_url=config.api.redoc_url
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.api.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Key security for admin endpoints
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: str = Security(api_key_header)):
    """Verify API key for admin endpoints"""
    expected_key = os.getenv("ADMIN_API_KEY")
    if not expected_key:
        # If no API key is set, allow localhost only
        return None
    
    if api_key != expected_key:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return api_key

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
        return config.i18n.default_locale
    
    languages = []
    for lang in accept_language.split(","):
        parts = lang.strip().split(";")
        locale = parts[0].split("-")[0]
        languages.append(locale)
    
    for locale in languages:
        if locale in config.i18n.supported_locales:
            return locale
    
    return config.i18n.default_locale

def get_i18n(locale: str):
    """Get i18n helper function with bound locale"""
    return functools.partial(i18n.get, locale=locale)

class SecurityValidator:
    """Validate URL security (SSRF protection)"""
    
    @staticmethod
    def validate_url(url: str) -> None:
        """Validate URL against SSRF attacks"""
        if not config.security.enable_ssrf_protection:
            return
        
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            
            if not hostname:
                raise ValueError("Invalid URL")
            
            # Get all resolved IPs (IPv4 + IPv6)
            try:
                addr_info = socket.getaddrinfo(hostname, None)
                ips = [info[4][0] for info in addr_info]
            except socket.gaierror:
                # DNS resolution failed - let yt-dlp handle it
                return
            
            # Check all resolved IPs
            for ip_str in ips:
                try:
                    ip = ipaddress.ip_address(ip_str)
                    
                    if not config.security.allow_localhost and ip.is_loopback:
                        raise ValueError(i18n.get("error.private_ip"))
                    
                    if not config.security.allow_private_ips and ip.is_private:
                        raise ValueError(i18n.get("error.private_ip"))
                    
                    if ip.is_link_local or ip.is_multicast:
                        raise ValueError(i18n.get("error.private_ip"))
                        
                except ValueError as e:
                    if "does not appear to be" in str(e):
                        continue
                    raise
                    
        except Exception as e:
            logger.error(f"URL validation failed: {str(e)}")
            raise ValueError(i18n.get("error.invalid_url", reason=str(e)))

class FormatPolicy:
    """Format selection policy"""
    
    @staticmethod
    def get_format_string(
        custom_format: Optional[str],
        audio_only: bool,
        audio_format: str,
        quality: Optional[int]
    ) -> Tuple[str, str, str]:
        """
        Get format string, extension, and MIME type
        Returns: (format_str, ext, media_type)
        """
        if custom_format:
            return custom_format, 'mp4', 'application/octet-stream'
        
        if audio_only:
            if audio_format == "mp3":
                return 'bestaudio', 'mp3', 'audio/mpeg'
            elif audio_format == "m4a":
                return 'bestaudio[ext=m4a]/bestaudio', 'm4a', 'audio/mp4'
            else:  # opus
                return 'bestaudio[ext=webm]/bestaudio', 'webm', 'audio/webm'
        
        if quality:
            format_str = (
                f"bestvideo[ext=mp4][height<={quality}]+bestaudio[ext=m4a]/"
                f"best[ext=mp4][height<={quality}]/best"
            )
            return format_str, 'mp4', 'application/octet-stream'
        
        return config.ytdlp.default_format, 'mp4', 'application/octet-stream'

class YTDLPCommandBuilder:
    """Build yt-dlp commands"""
    
    @staticmethod
    def build_info_command(url: str, js_runtime: Optional[str]) -> List[str]:
        """Build command for fetching video info"""
        cmd = [
            'yt-dlp',
            '--dump-json',
            '--no-playlist',
            '--socket-timeout', str(config.download.socket_timeout),
            '--retries', str(config.download.retries),
        ]
        
        if not config.ytdlp.enable_live_streams:
            cmd.extend(['--match-filter', '!is_live'])
        
        if js_runtime:
            cmd.extend(['--js-runtimes', js_runtime])
        
        cmd.append(url)
        
        return cmd
    
    @staticmethod
    def build_stream_command(
        url: str,
        format_str: str,
        js_runtime: Optional[str],
        audio_only: bool,
        audio_format: str
    ) -> List[str]:
        """Build command for streaming download"""
        cmd = [
            'yt-dlp',
            url,
            '-f', format_str,
            '-o', '-',
            '--no-playlist',
            '--socket-timeout', str(config.download.socket_timeout),
            '--retries', str(config.download.retries),
        ]
        
        if not config.ytdlp.enable_live_streams:
            cmd.extend(['--match-filter', '!is_live'])
        
        if js_runtime:
            cmd.extend(['--js-runtimes', js_runtime])
        
        # Only suppress progress, not warnings
        if config.logging.level != "DEBUG":
            cmd.append('--no-progress')
        
        if audio_only and audio_format == "mp3":
            cmd.extend(['-x', '--audio-format', 'mp3'])
        
        return cmd
    
    @staticmethod
    def build_filename_command(url: str, js_runtime: Optional[str]) -> List[str]:
        """Build command for getting filename"""
        cmd = [
            'yt-dlp',
            '--print', 'filename',
            '-o', '%(title)s.%(ext)s',
            '--no-playlist',
        ]
        
        if js_runtime:
            cmd.extend(['--js-runtimes', js_runtime])
        
        cmd.append(url)
        
        return cmd

class VideoRequest(BaseModel):
    url: HttpUrl = Field(..., description="Video URL")
    format: Optional[str] = Field(None, description="Custom format specification")
    audio_only: Optional[bool] = Field(False, description="Download audio only")
    audio_format: Optional[str] = Field(None, description="Audio format (opus/m4a/mp3)")
    quality: Optional[int] = Field(None, description="Video quality", ge=144, le=2160)
    
    @validator('audio_format', pre=True, always=True)
    def set_default_audio_format(cls, v):
        return v or config.ytdlp.default_audio_format
    
    @validator('url')
    def validate_url_safety(cls, v):
        """SSRF protection"""
        SecurityValidator.validate_url(str(v))
        return v

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
    """Sanitize filename"""
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
    """Safe URL for logging"""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except:
        return "invalid_url"

def hash_user_agent(user_agent: str) -> str:
    """Create stable hash of user agent"""
    return hashlib.sha256(user_agent.encode()).hexdigest()[:8]

class RedisRateLimiter:
    """Redis-based rate limiter with Lua script"""
    
    def __init__(self):
        self.max_requests = config.rate_limit.max_requests
        self.window_seconds = config.rate_limit.window_seconds
        
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
        if not config.rate_limit.enabled or not REDIS_AVAILABLE or redis_client is None:
            return True
        
        client_ip = request.client.host
        endpoint = request.url.path
        user_agent = request.headers.get("user-agent", "")
        ua_hash = hash_user_agent(user_agent)
        
        key = f"rate:{client_ip}:{endpoint}:{ua_hash}"
        
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
                _ = get_i18n(locale)
                raise HTTPException(
                    status_code=429,
                    detail=_("error.rate_limit", seconds=ttl),
                    headers={"Retry-After": str(ttl)}
                )
            
            return True
        except HTTPException:
            raise
        except Exception as e:
            logger.error(i18n.get("log.redis_rate_limit_error", error=str(e)))
            return True

class ConcurrencyLimiter:
    """Concurrent download limiter with TTL"""
    
    async def __call__(self, request: Request):
        if not REDIS_AVAILABLE or redis_client is None:
            return True
        
        request_id = str(uuid.uuid4())
        key = f"active_download:{request_id}"
        global_key = "active_downloads_count"
        
        try:
            # Check current count
            current = int(await redis_client.get(global_key) or 0)
            
            if current >= config.download.max_concurrent:
                locale = get_locale(request.headers.get("accept-language"))
                _ = get_i18n(locale)
                raise HTTPException(
                    status_code=503,
                    detail=_("error.server_busy", max=config.download.max_concurrent)
                )
            
            # Register this download with TTL (auto-cleanup after timeout)
            ttl = config.download.timeout_seconds + 60  # Add buffer
            await redis_client.setex(key, ttl, "1")
            await redis_client.incr(global_key)
            
            # Store for cleanup
            request.state.download_slot_key = key
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
    if not hasattr(request.state, 'download_slot_acquired') or not request.state.download_slot_acquired:
        return
    
    if redis_client and hasattr(request.state, 'download_slot_key'):
        try:
            # Delete individual key
            await redis_client.delete(request.state.download_slot_key)
            # Decrement global counter
            await redis_client.decr("active_downloads_count")
        except Exception as e:
            logger.error(i18n.get("log.slot_release_failed", error=str(e)))

async def detect_js_runtime() -> Optional[str]:
    """Auto-detect JS runtime"""
    if not config.ytdlp.auto_detect_runtime:
        return None
    
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
    """Check Deno installation"""
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
    """Load supported sites (with Redis cache)"""
    global SUPPORTED_SITES
    
    # Try Redis cache first
    if redis_client:
        try:
            cached = await redis_client.get("supported_sites:cache")
            if cached:
                SUPPORTED_SITES = json.loads(cached)
                console.print(f"[green]Loaded {len(SUPPORTED_SITES)} sites from cache[/green]")
                logger.info(f"Loaded {len(SUPPORTED_SITES)} extractors from cache")
                return
        except Exception as e:
            logger.warning(f"Failed to load from Redis cache: {str(e)}")
    
    # Load from yt-dlp
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--list-extractors',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15.0)
        SUPPORTED_SITES = stdout.decode().strip().split('\n')
        
        # Cache in Redis (1 day TTL)
        if redis_client:
            try:
                await redis_client.setex(
                    "supported_sites:cache",
                    86400,
                    json.dumps(SUPPORTED_SITES)
                )
            except Exception as e:
                logger.warning(f"Failed to cache in Redis: {str(e)}")
        
        console.print(f"[green]{i18n.get('startup.sites_cached', count=len(SUPPORTED_SITES))}[/green]")
        logger.info(i18n.get("startup.sites_cached", count=len(SUPPORTED_SITES)))
    except Exception as e:
        console.print(f"[yellow]{i18n.get('startup.sites_failed')}[/yellow]")
        logger.warning(i18n.get("log.extractor_load_failed", error=str(e)))

@app.on_event("startup")
async def startup_event():
    """Startup handler"""
    global redis_client, DENO_VERSION, YTDLP_VERSION, JS_RUNTIME_PATH
    
    console.rule(f"[bold blue]{config.api.title} v{config.api.version}[/bold blue]")
    
    # Display configuration
    console.print(f"[cyan]Configuration:[/cyan]")
    console.print(f"  Redis: {config.redis.url}")
    console.print(f"  Rate limit: {config.rate_limit.max_requests} req/{config.rate_limit.window_seconds}s (enabled: {config.rate_limit.enabled})")
    console.print(f"  Max concurrent: {config.download.max_concurrent}")
    console.print(f"  Timeout: {config.download.timeout_seconds}s")
    console.print(f"  SSRF protection: {config.security.enable_ssrf_protection}")
    console.print(f"  Locale: {config.i18n.default_locale}")
    console.print(f"  Log level: {config.logging.level}")
    
    # JS runtime detection
    if config.ytdlp.js_runtime:
        JS_RUNTIME_PATH = config.ytdlp.js_runtime
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
                config.redis.url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=config.redis.socket_timeout
            )
            await redis_client.ping()
            
            # Recovery: reset active downloads counter on startup
            await redis_client.set("active_downloads_count", 0)
            console.print(f"[green]{i18n.get('startup.redis_connected')} (counter reset)[/green]")
            logger.info(i18n.get("startup.redis_connected"))
        except Exception as e:
            console.print(f"[yellow]{i18n.get('startup.redis_failed', error=str(e))}[/yellow]")
            logger.warning(i18n.get("startup.redis_failed", error=str(e)))
            redis_client = None
    
    # Load supported sites
    asyncio.create_task(load_supported_sites())
    
    console.rule(f"[bold green]{i18n.get('startup.api_ready')}[/bold green]")

@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown handler"""
    console.rule(f"[bold yellow]{i18n.get('shutdown.closing')}[/bold yellow]")
    
    if redis_client:
        await redis_client.close()
        logger.info(i18n.get("shutdown.redis_closed"))

@app.get("/", tags=["Health"])
async def root():
    """Root endpoint"""
    return {
        "status": i18n.get("response.status_running"),
        "service": config.api.title,
        "version": config.api.version,
        "deno_version": DENO_VERSION,
        "ytdlp_version": YTDLP_VERSION,
        "redis_enabled": redis_client is not None,
        "config": {
            "max_concurrent_downloads": config.download.max_concurrent,
            "rate_limit_enabled": config.rate_limit.enabled,
            "ssrf_protection": config.security.enable_ssrf_protection
        }
    }

@app.get("/health", tags=["Health"])
async def health_check():
    """Lightweight health check"""
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
    
    deno_info = await check_deno_installation()
    
    redis_status = i18n.get("response.redis_disabled")
    active_downloads = 0
    if redis_client:
        try:
            await redis_client.ping()
            redis_status = i18n.get("response.redis_connected")
            active_downloads = int(await redis_client.get("active_downloads_count") or 0)
        except:
            redis_status = i18n.get("response.redis_disconnected")
    
    health_data = {
        "status": i18n.get("health.status"),
        "ytdlp_version": ytdlp_version,
        "deno": deno_info,
        "redis_status": redis_status,
        "js_runtime": JS_RUNTIME_PATH,
        "active_downloads": active_downloads,
        "max_downloads": config.download.max_concurrent,
        "supported_sites_loaded": len(SUPPORTED_SITES)
    }
    
    if deno_info["status"] != i18n.get("response.deno_installed"):
        health_data["warning"] = i18n.get("health.warning_no_deno")
    
    return health_data

@app.get("/config", tags=["Admin"], dependencies=[Depends(verify_api_key)])
async def get_config():
    """Get current configuration (admin only)"""
    return {
        "rate_limit": config.rate_limit.dict(),
        "download": config.download.dict(),
        "security": config.security.dict(),
        "ytdlp": config.ytdlp.dict(),
        "i18n": config.i18n.dict(),
        "api": {
            "title": config.api.title,
            "version": config.api.version
        }
    }

@app.post("/info", response_model=VideoInfo, tags=["Video Info"], dependencies=[Depends(rate_limiter)])
async def get_video_info(request: Request, video_request: VideoRequest):
    """Get video information"""
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = get_i18n(locale)
    
    cmd = YTDLPCommandBuilder.build_info_command(
        str(video_request.url),
        JS_RUNTIME_PATH
    )
    
    safe_url = safe_url_for_log(str(video_request.url))
    logger.info(_("log.fetching_info", url=safe_url))
    
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
            logger.error(_("log.info_timeout"))
            raise HTTPException(status_code=504, detail=_("error.timeout"))
        
        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error(_("log.ytdlp_error", code=process.returncode, message=error_msg))
            raise HTTPException(status_code=400, detail=_("error.fetch_info_failed", reason=error_msg[:200]))
        
        info = json.loads(stdout.decode())
        
        is_live = info.get('is_live', False)
        if is_live and not config.ytdlp.enable_live_streams:
            raise HTTPException(status_code=400, detail=_("error.live_not_supported"))
        
        logger.info(_("log.info_retrieved", title=info.get('title', 'Unknown')))
        
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
        logger.error(_("log.json_parse_error", error=str(e)))
        raise HTTPException(status_code=500, detail=_("error.parse_failed"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Video info error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/stream", tags=["Download"], dependencies=[Depends(rate_limiter), Depends(concurrency_limiter)])
async def stream_video(request: Request, video_request: VideoRequest):
    """Stream video download"""
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = get_i18n(locale)
    
    # Get format details
    format_str, ext, media_type = FormatPolicy.get_format_string(
        video_request.format,
        video_request.audio_only,
        video_request.audio_format,
        video_request.quality
    )
    
    # Generate simple filename (avoid extra yt-dlp call)
    video_id = hashlib.md5(str(video_request.url).encode()).hexdigest()[:8]
    filename = f"video_{video_id}.{ext}"
    
    cmd = YTDLPCommandBuilder.build_stream_command(
        str(video_request.url),
        format_str,
        JS_RUNTIME_PATH,
        video_request.audio_only,
        video_request.audio_format
    )
    
    safe_url = safe_url_for_log(str(video_request.url))
    logger.info(_("log.starting_stream", url=safe_url, format=format_str))
    
    process = None
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        stderr_lines = deque(maxlen=50)  # Keep only last 50 lines
        
        async def drain_stderr():
            """Drain stderr to prevent buffer deadlock"""
            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    stderr_lines.append(decoded)
                    if config.logging.level == "DEBUG":
                        logger.debug(f"yt-dlp: {decoded}")
            except:
                pass
        
        stderr_task = asyncio.create_task(drain_stderr())
        
        async def generate():
            """Stream generator"""
            CHUNK_SIZE = 1024 * 1024
            try:
                while True:
                    chunk = await process.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            except asyncio.CancelledError:
                logger.warning(_("log.client_disconnected", pid=process.pid))
                process.kill()
                await process.wait()
                raise
            except Exception as e:
                logger.error(_("log.stream_error", error=str(e)))
                process.kill()
                await process.wait()
                raise
            finally:
                try:
                    returncode = await asyncio.wait_for(process.wait(), timeout=5.0)
                    
                    if returncode != 0:
                        error_summary = '\n'.join(stderr_lines)
                        logger.error(_("log.process_exit_error", code=returncode))
                        if error_summary:
                            logger.error(f"stderr: {error_summary}")
                    
                except asyncio.TimeoutError:
                    logger.warning(_("log.process_timeout"))
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
        
        logger.error(_("log.stream_error", error=str(e)))
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
    """HTTP exception handler"""
    
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
    """General exception handler"""
    
    await release_download_slot(request)
    
    logger.exception(i18n.get("log.unhandled_exception", error=str(exc)))
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = get_i18n(locale)
    
    # In DEBUG mode, return full traceback
    if config.logging.level == "DEBUG":
        import traceback
        return JSONResponse(
            status_code=500,
            content={
                "error": True,
                "status_code": 500,
                "detail": _("error.internal"),
                "exception": str(exc),
                "traceback": traceback.format_exc(),
                "timestamp": datetime.now().isoformat()
            }
        )
    
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "status_code": 500,
            "detail": _("error.internal"),
            "timestamp": datetime.now().isoformat()
        }
    )
