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
from contextlib import suppress
import heapq

# Rich logging
from rich.logging import RichHandler
from rich.console import Console
import logging

# Configuration
from config import config

# i18n
from i18n import i18n

console = Console()

# Constants
CHUNK_SIZE = 4 * 1024 * 1024  # 4MB
INFO_CACHE_TTL = 300  # 5 minutes
SSRF_CACHE_TTL = 300
STDERR_MAX_LINES = 50

# Setup logging
if config.logging.enable_rich:
    logging.basicConfig(
        level=getattr(logging, config.logging.level),
        format="%(message)s",
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

# API Key security
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(api_key: str = Security(api_key_header)):
    """Verify API key for admin endpoints"""
    expected_key = os.getenv("ADMIN_API_KEY")
    if not expected_key:
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

# Global state (immutable after initialization)
SUPPORTED_SITES: Tuple[str, ...] = ()
SUPPORTED_SITES_ETAG: str = ""
redis_client: Optional[aioredis.Redis] = None
DENO_VERSION: str = "unknown"
YTDLP_VERSION: str = "unknown"
JS_RUNTIME_PATH: Optional[str] = None

# ============================================================================
# Middleware: Request ID
# ============================================================================

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Add request ID for tracing"""
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    
    return response

# ============================================================================
# Utilities
# ============================================================================

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
    """Safe URL for logging"""
    try:
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        
        if config.logging.level == "DEBUG" and parsed.query:
            return f"{base_url}?..."
        
        return base_url
    except:
        return "invalid_url"

def hash_stable(data: str) -> str:
    """Create stable hash"""
    return hashlib.sha256(data.encode()).hexdigest()[:16]

# ============================================================================
# Security Validator
# ============================================================================

class SecurityValidator:
    """Validate URL security with caching"""
    
    @staticmethod
    async def validate_url(url: str) -> None:
        """
        Validate URL against SSRF attacks.
        Uses async DNS resolution to prevent event loop blocking.
        Results are cached in Redis to reduce DNS queries.
        """
        if not config.security.enable_ssrf_protection:
            return
        
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            
            if not hostname:
                raise ValueError("Invalid URL")
            
            # Check cache first
            if redis_client:
                cache_key = f"ssrf:{hash_stable(hostname)}"
                cached = await redis_client.get(cache_key)
                if cached == "ok":
                    return
                if cached == "blocked":
                    raise ValueError(i18n.get("error.private_ip"))
            
            # Async DNS resolution to prevent event loop blocking
            try:
                addr_info = await asyncio.to_thread(
                    socket.getaddrinfo,
                    hostname,
                    None
                )
                ips = [info[4][0] for info in addr_info]
            except socket.gaierror:
                # DNS resolution failed - cache as OK and let yt-dlp handle it
                if redis_client:
                    await redis_client.setex(cache_key, SSRF_CACHE_TTL, "ok")
                return
            
            # Check all resolved IPs
            is_blocked = False
            for ip_str in ips:
                try:
                    ip = ipaddress.ip_address(ip_str)
                    
                    if not config.security.allow_localhost and ip.is_loopback:
                        is_blocked = True
                        break
                    
                    if not config.security.allow_private_ips and ip.is_private:
                        is_blocked = True
                        break
                    
                    if ip.is_link_local or ip.is_multicast:
                        is_blocked = True
                        break
                        
                except ValueError as e:
                    if "does not appear to be" in str(e):
                        continue
                    raise
            
            # Cache result
            if redis_client:
                await redis_client.setex(
                    cache_key,
                    SSRF_CACHE_TTL,
                    "blocked" if is_blocked else "ok"
                )
            
            if is_blocked:
                raise ValueError(i18n.get("error.private_ip"))
                    
        except Exception as e:
            logger.error(f"URL validation failed: {str(e)}")
            raise ValueError(i18n.get("error.invalid_url", reason=str(e)))

# ============================================================================
# Models
# ============================================================================

class DownloadIntent(BaseModel):
    """Internal download intent (separated from HTTP concerns)"""
    url: str
    audio_only: bool
    audio_format: str
    quality: Optional[int]
    custom_format: Optional[str]

class MediaMetadata(BaseModel):
    """Media metadata"""
    format_str: str
    ext: str
    media_type: str

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
    def validate_url_syntax(cls, v):
        """Validate URL syntax only (SSRF check done at endpoint)"""
        parsed = urlparse(str(v))
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("Invalid URL format")
        return v
    
    def to_intent(self) -> DownloadIntent:
        """Convert to download intent"""
        return DownloadIntent(
            url=str(self.url),
            audio_only=self.audio_only,
            audio_format=self.audio_format,
            quality=self.quality,
            custom_format=self.format
        )

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

# ============================================================================
# Format Decision
# ============================================================================

class FormatDecision:
    """Make format decisions"""
    
    @staticmethod
    def decide(intent: DownloadIntent) -> str:
        """Decide format string based on intent"""
        if intent.custom_format:
            return intent.custom_format
        
        if intent.audio_only:
            if intent.audio_format == "mp3":
                return 'bestaudio'
            elif intent.audio_format == "m4a":
                return 'bestaudio[ext=m4a]/bestaudio'
            else:  # opus
                return 'bestaudio[ext=webm]/bestaudio'
        
        if intent.quality:
            return (
                f"bestvideo[ext=mp4][height<={intent.quality}]+bestaudio[ext=m4a]/"
                f"best[ext=mp4][height<={intent.quality}]/best"
            )
        
        return config.ytdlp.default_format
    
    @staticmethod
    def get_metadata(intent: DownloadIntent) -> MediaMetadata:
        """Get media metadata based on intent"""
        format_str = FormatDecision.decide(intent)
        
        if intent.custom_format:
            return MediaMetadata(
                format_str=format_str,
                ext='mp4',
                media_type='application/octet-stream'
            )
        
        if intent.audio_only:
            if intent.audio_format == "mp3":
                return MediaMetadata(format_str=format_str, ext='mp3', media_type='audio/mpeg')
            elif intent.audio_format == "m4a":
                return MediaMetadata(format_str=format_str, ext='m4a', media_type='audio/mp4')
            else:
                return MediaMetadata(format_str=format_str, ext='webm', media_type='audio/webm')
        
        return MediaMetadata(
            format_str=format_str,
            ext='mp4',
            media_type='application/octet-stream'
        )

# ============================================================================
# YT-DLP Command Builder
# ============================================================================

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
        audio_format: str,
        include_filesize: bool = True
    ) -> List[str]:
        """
        Build command for streaming download.
        Includes --print filesize_approx to get Content-Length in one call.
        """
        cmd = [
            'yt-dlp',
            url,
            '-f', format_str,
            '-o', '-',
            '--no-playlist',
            '--socket-timeout', str(config.download.socket_timeout),
            '--retries', str(config.download.retries),
        ]
        
        # Print filesize before streaming
        if include_filesize:
            cmd.extend(['--print', 'before_dl:%(filesize_approx)s'])
        
        if not config.ytdlp.enable_live_streams:
            cmd.extend(['--match-filter', '!is_live'])
        
        if js_runtime:
            cmd.extend(['--js-runtimes', js_runtime])
        
        if config.logging.level != "DEBUG":
            cmd.append('--no-progress')
        
        if audio_only and audio_format == "mp3":
            cmd.extend(['-x', '--audio-format', 'mp3'])
        
        return cmd

# ============================================================================
# Rate Limiter
# ============================================================================

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
        
        # Simplified key (removed UA hash to reduce Redis keys)
        key = f"rate:{client_ip}:{endpoint}"
        
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
            logger.error(i18n.get("log.redis_rate_limit_error", error=str(e)), extra={"request_id": request.state.request_id})
            return True

# ============================================================================
# Concurrency Limiter
# ============================================================================

class ConcurrencyLimiter:
    """
    Concurrent download limiter with atomic operations.
    Uses Lua script to prevent TOCTOU races.
    Counter has TTL for auto-cleanup.
    """
    
    def __init__(self):
        self.lua_script = """
        local counter_key = KEYS[1]
        local slot_key = KEYS[2]
        local limit = tonumber(ARGV[1])
        local slot_ttl = tonumber(ARGV[2])
        local counter_ttl = tonumber(ARGV[3])
        
        local current = tonumber(redis.call('GET', counter_key) or "0")
        if current >= limit then
            return 0
        end
        
        redis.call('INCR', counter_key)
        redis.call('EXPIRE', counter_key, counter_ttl)
        redis.call('SETEX', slot_key, slot_ttl, "1")
        
        return 1
        """
    
    async def __call__(self, request: Request):
        if not REDIS_AVAILABLE or redis_client is None:
            return True
        
        request_id = str(uuid.uuid4())
        counter_key = "active_downloads_count"
        slot_key = f"active_download:{request_id}"
        slot_ttl = config.download.timeout_seconds + 60
        counter_ttl = slot_ttl * 2  # Counter TTL = 2x slot TTL for safety
        
        try:
            allowed = await redis_client.eval(
                self.lua_script,
                2,
                counter_key,
                slot_key,
                config.download.max_concurrent,
                slot_ttl,
                counter_ttl
            )
            
            if not allowed:
                locale = get_locale(request.headers.get("accept-language"))
                _ = get_i18n(locale)
                raise HTTPException(
                    status_code=503,
                    detail=_("error.server_busy", max=config.download.max_concurrent)
                )
            
            request.state.download_slot_key = slot_key
            request.state.download_slot_acquired = True
            
            return True
        except HTTPException:
            raise
        except Exception as e:
            logger.error(i18n.get("log.concurrency_error", error=str(e)), extra={"request_id": request.state.request_id})
            return True

rate_limiter = RedisRateLimiter()
concurrency_limiter = ConcurrencyLimiter()

async def release_download_slot(request: Request):
    """Release download slot"""
    if not hasattr(request.state, 'download_slot_acquired') or not request.state.download_slot_acquired:
        return
    
    if redis_client and hasattr(request.state, 'download_slot_key'):
        try:
            await redis_client.delete(request.state.download_slot_key)
            await redis_client.decr("active_downloads_count")
        except Exception as e:
            logger.error(i18n.get("log.slot_release_failed", error=str(e)), extra={"request_id": request.state.request_id})

# ============================================================================
# Initialization Functions
# ============================================================================

async def init_redis():
    """Initialize Redis connection with recovery"""
    global redis_client
    
    if not REDIS_AVAILABLE:
        return
    
    try:
        redis_client = await aioredis.from_url(
            config.redis.url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=config.redis.socket_timeout
        )
        await redis_client.ping()
        
        # Recover active downloads counter
        keys = []
        cursor = 0
        while True:
            cursor, partial_keys = await redis_client.scan(
                cursor,
                match="active_download:*",
                count=100
            )
            keys.extend(partial_keys)
            if cursor == 0:
                break
        
        await redis_client.set("active_downloads_count", len(keys))
        
        if len(keys) > 0:
            console.print(f"[yellow]Recovered {len(keys)} active downloads[/yellow]")
            logger.info(f"Recovered {len(keys)} active download slots")
        else:
            console.print(f"[green]Active downloads counter initialized[/green]")
        
        console.print(f"[green]{i18n.get('startup.redis_connected')}[/green]")
        logger.info(i18n.get("startup.redis_connected"))
        
    except Exception as e:
        console.print(f"[yellow]{i18n.get('startup.redis_failed', error=str(e))}[/yellow]")
        logger.warning(i18n.get("startup.redis_failed", error=str(e)))
        redis_client = None

async def init_ytdlp():
    """Initialize yt-dlp and check version"""
    global YTDLP_VERSION
    
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

async def init_runtime():
    """Initialize JS runtime detection"""
    global JS_RUNTIME_PATH, DENO_VERSION
    
    if config.ytdlp.js_runtime:
        JS_RUNTIME_PATH = config.ytdlp.js_runtime
        console.print(f"[cyan]{i18n.get('startup.js_runtime_configured', runtime=JS_RUNTIME_PATH)}[/cyan]")
    elif config.ytdlp.auto_detect_runtime:
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
                        JS_RUNTIME_PATH = f"{runtime_name}:{runtime_path}"
                        logger.info(i18n.get("log.js_runtime_detected", name=runtime_name, path=runtime_path))
                        console.print(f"[green]{i18n.get('startup.js_runtime_detected', runtime=JS_RUNTIME_PATH)}[/green]")
                        break
                except:
                    pass
        
        if not JS_RUNTIME_PATH:
            console.print(f"[yellow]{i18n.get('startup.js_runtime_none')}[/yellow]")
    
    # Check Deno specifically
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
                DENO_VERSION = line.split()[1]
                console.print(f"[green]{i18n.get('startup.deno_detected', version=DENO_VERSION)}[/green]")
                break
    except:
        pass

async def init_supported_sites():
    """Load supported sites with Redis cache"""
    global SUPPORTED_SITES, SUPPORTED_SITES_ETAG
    
    # Try Redis cache first
    if redis_client:
        try:
            cached = await redis_client.get("supported_sites:cache")
            if cached:
                sites_list = json.loads(cached)
                SUPPORTED_SITES = tuple(sites_list)  # Immutable
                SUPPORTED_SITES_ETAG = hashlib.sha256(cached.encode()).hexdigest()[:16]
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
        sites_list = stdout.decode().strip().split('\n')
        SUPPORTED_SITES = tuple(sites_list)  # Immutable
        
        sites_json = json.dumps(sites_list)
        SUPPORTED_SITES_ETAG = hashlib.sha256(sites_json.encode()).hexdigest()[:16]
        
        # Cache in Redis (1 day TTL)
        if redis_client:
            try:
                await redis_client.setex("supported_sites:cache", 86400, sites_json)
            except Exception as e:
                logger.warning(f"Failed to cache in Redis: {str(e)}")
        
        console.print(f"[green]{i18n.get('startup.sites_cached', count=len(SUPPORTED_SITES))}[/green]")
        logger.info(i18n.get("startup.sites_cached", count=len(SUPPORTED_SITES)))
    except Exception as e:
        console.print(f"[yellow]{i18n.get('startup.sites_failed')}[/yellow]")
        logger.warning(i18n.get("log.extractor_load_failed", error=str(e)))

# ============================================================================
# Startup / Shutdown
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Startup handler"""
    
    console.rule(f"[bold blue]{config.api.title} v{config.api.version}[/bold blue]")
    
    # Display configuration
    console.print(f"[cyan]Configuration:[/cyan]")
    console.print(f"  Redis: {config.redis.url}")
    console.print(f"  Rate limit: {config.rate_limit.max_requests} req/{config.rate_limit.window_seconds}s")
    console.print(f"  Max concurrent: {config.download.max_concurrent}")
    console.print(f"  Timeout: {config.download.timeout_seconds}s")
    console.print(f"  SSRF protection: {config.security.enable_ssrf_protection}")
    console.print(f"  Locale: {config.i18n.default_locale}")
    console.print(f"  Log level: {config.logging.level}")
    
    # Initialize components
    await init_redis()
    await init_ytdlp()
    await init_runtime()
    asyncio.create_task(init_supported_sites())
    
    console.rule(f"[bold green]{i18n.get('startup.api_ready')}[/bold green]")

@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown handler"""
    console.rule(f"[bold yellow]{i18n.get('shutdown.closing')}[/bold yellow]")
    
    if redis_client:
        await redis_client.close()
        logger.info(i18n.get("shutdown.redis_closed"))

# ============================================================================
# Endpoints
# ============================================================================

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
    
    redis_status = i18n.get("response.redis_disabled")
    active_downloads = 0
    if redis_client:
        try:
            await redis_client.ping()
            redis_status = i18n.get("response.redis_connected")
            active_downloads = int(await redis_client.get("active_downloads_count") or 0)
        except:
            redis_status = i18n.get("response.redis_disconnected")
    
    return {
        "status": i18n.get("health.status"),
        "ytdlp_version": ytdlp_version,
        "deno_version": DENO_VERSION,
        "redis_status": redis_status,
        "js_runtime": JS_RUNTIME_PATH,
        "active_downloads": active_downloads,
        "max_downloads": config.download.max_concurrent,
        "supported_sites_loaded": len(SUPPORTED_SITES)
    }

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
    """
    Get video information with Redis caching.
    Cache key is based on URL hash to prevent bot abuse.
    """
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = get_i18n(locale)
    
    # SSRF check (moved from validator to endpoint)
    await SecurityValidator.validate_url(str(video_request.url))
    
    # Check cache
    cache_key = f"info:{hash_stable(str(video_request.url))}"
    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                logger.info("Video info served from cache", extra={
                    "request_id": request.state.request_id,
                    "url": safe_url_for_log(str(video_request.url))
                })
                return VideoInfo(**json.loads(cached))
        except Exception as e:
            logger.warning(f"Cache read failed: {str(e)}", extra={"request_id": request.state.request_id})
    
    cmd = YTDLPCommandBuilder.build_info_command(
        str(video_request.url),
        JS_RUNTIME_PATH
    )
    
    safe_url = safe_url_for_log(str(video_request.url))
    logger.info(_("log.fetching_info", url=safe_url), extra={"request_id": request.state.request_id})
    
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
            logger.error(_("log.info_timeout"), extra={"request_id": request.state.request_id})
            raise HTTPException(status_code=504, detail=_("error.timeout"))
        
        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error(_("log.ytdlp_error", code=process.returncode, message=error_msg), extra={"request_id": request.state.request_id})
            raise HTTPException(status_code=400, detail=_("error.fetch_info_failed", reason=error_msg[:200]))
        
        info = json.loads(stdout.decode())
        
        is_live = info.get('is_live', False)
        if is_live and not config.ytdlp.enable_live_streams:
            raise HTTPException(status_code=400, detail=_("error.live_not_supported"))
        
        logger.info(_("log.info_retrieved", title=info.get('title', 'Unknown')), extra={"request_id": request.state.request_id})
        
        # Use heapq for efficient top-N selection
        all_formats = info.get("formats", [])
        formats = heapq.nlargest(
            20,
            all_formats,
            key=lambda f: (f.get("height") or 0, f.get("filesize") or 0)
        )
        
        video_info = VideoInfo(
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
                for f in formats
            ],
            thumbnail=info.get("thumbnail"),
            uploader=info.get("uploader"),
            webpage_url=info.get("webpage_url", str(video_request.url)),
            is_live=is_live
        )
        
        # Cache result
        if redis_client:
            try:
                await redis_client.setex(
                    cache_key,
                    INFO_CACHE_TTL,
                    video_info.json()
                )
            except Exception as e:
                logger.warning(f"Cache write failed: {str(e)}", extra={"request_id": request.state.request_id})
        
        return video_info
        
    except json.JSONDecodeError as e:
        logger.error(_("log.json_parse_error", error=str(e)), extra={"request_id": request.state.request_id})
        raise HTTPException(status_code=500, detail=_("error.parse_failed"))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Video info error: {str(e)}", extra={"request_id": request.state.request_id}, exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/stream", tags=["Download"], dependencies=[Depends(rate_limiter), Depends(concurrency_limiter)])
async def stream_video(request: Request, video_request: VideoRequest):
    """
    Stream video download with optimized single yt-dlp call.
    Uses --print before_dl to get filesize without extra process.
    """
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = get_i18n(locale)
    
    # SSRF check
    await SecurityValidator.validate_url(str(video_request.url))
    
    # Convert to intent and get metadata
    intent = video_request.to_intent()
    metadata = FormatDecision.get_metadata(intent)
    
    # Generate filename
    video_id = hash_stable(intent.url)[:8]
    filename = f"video_{video_id}.{metadata.ext}"
    
    cmd = YTDLPCommandBuilder.build_stream_command(
        intent.url,
        metadata.format_str,
        JS_RUNTIME_PATH,
        intent.audio_only,
        intent.audio_format,
        include_filesize=True
    )
    
    safe_url = safe_url_for_log(intent.url)
    logger.info(_("log.starting_stream", url=safe_url, format=metadata.format_str), extra={"request_id": request.state.request_id})
    
    process = None
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        # Read filesize from first line (--print before_dl)
        content_length = None
        try:
            first_line = await asyncio.wait_for(process.stdout.readline(), timeout=10.0)
            size_str = first_line.decode().strip()
            if size_str.isdigit():
                content_length = int(size_str)
        except:
            pass
        
        stderr_lines = deque(maxlen=STDERR_MAX_LINES)
        
        async def drain_stderr():
            """Drain stderr to prevent buffer deadlock"""
            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    # Only collect stderr if we'll need it (on error)
                    if process.returncode is None or process.returncode != 0:
                        stderr_lines.append(decoded)
                    if config.logging.level == "DEBUG":
                        logger.debug(f"yt-dlp: {decoded}", extra={"request_id": request.state.request_id})
            except:
                pass
        
        stderr_task = asyncio.create_task(drain_stderr())
        
        async def generate():
            """Stream generator with optimized chunk size"""
            try:
                while True:
                    chunk = await process.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            except asyncio.CancelledError:
                logger.warning(_("log.client_disconnected", pid=process.pid), extra={"request_id": request.state.request_id})
                process.kill()
                await process.wait()
                raise
            except Exception as e:
                logger.error(_("log.stream_error", error=str(e)), extra={"request_id": request.state.request_id}, exc_info=True)
                process.kill()
                await process.wait()
                raise
            finally:
                try:
                    returncode = await asyncio.wait_for(process.wait(), timeout=5.0)
                    
                    if returncode != 0:
                        error_summary = '\n'.join(stderr_lines)
                        logger.error(_("log.process_exit_error", code=returncode), extra={"request_id": request.state.request_id})
                        if error_summary:
                            logger.error(f"stderr: {error_summary}", extra={"request_id": request.state.request_id})
                    
                except asyncio.TimeoutError:
                    logger.warning(_("log.process_timeout"), extra={"request_id": request.state.request_id})
                    process.kill()
                    await process.wait()
                
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task
                
                await release_download_slot(request)
        
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'no-cache',
            'Accept-Ranges': 'none',  # Streaming mode doesn't support ranges
        }
        
        if content_length:
            headers['Content-Length'] = str(content_length)
        
        return StreamingResponse(
            generate(),
            media_type=metadata.media_type,
            headers=headers
        )
        
    except Exception as e:
        await release_download_slot(request)
        
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        
        logger.error(_("log.stream_error", error=str(e)), extra={"request_id": request.state.request_id}, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/supported-sites", tags=["Info"])
async def get_supported_sites(
    request: Request,
    limit: int = 50,
    search: Optional[str] = None
):
    """Get supported sites list with ETag support"""
    
    # Check ETag
    if_none_match = request.headers.get("if-none-match")
    if if_none_match == f'"{SUPPORTED_SITES_ETAG}"':
        return JSONResponse(status_code=304, content={})
    
    sites = list(SUPPORTED_SITES)
    
    if search:
        search_lower = search.lower()
        sites = [s for s in sites if search_lower in s.lower()]
    
    response = JSONResponse(
        content={
            "count": len(sites),
            "total": len(SUPPORTED_SITES),
            "sites": sites[:limit]
        },
        headers={
            "ETag": f'"{SUPPORTED_SITES_ETAG}"',
            "Cache-Control": "public, max-age=3600"
        }
    )
    
    return response

# ============================================================================
# Exception Handlers
# ============================================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTP exception handler"""
    
    await release_download_slot(request)
    
    safe_url = safe_url_for_log(str(request.url))
    logger.error(
        i18n.get("log.http_error", code=exc.status_code, detail=exc.detail, url=safe_url),
        extra={"request_id": request.state.request_id}
    )
    
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": True,
            "status_code": exc.status_code,
            "detail": exc.detail,
            "request_id": request.state.request_id,
            "timestamp": datetime.now().isoformat()
        },
        headers=exc.headers or {}
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """General exception handler with DEBUG mode support"""
    
    await release_download_slot(request)
    
    logger.exception(
        i18n.get("log.unhandled_exception", error=str(exc)),
        extra={"request_id": request.state.request_id}
    )
    
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
                "request_id": request.state.request_id,
                "timestamp": datetime.now().isoformat()
            }
        )
    
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "status_code": 500,
            "detail": _("error.internal"),
            "request_id": request.state.request_id,
            "timestamp": datetime.now().isoformat()
        }
    )
