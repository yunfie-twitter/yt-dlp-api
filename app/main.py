from fastapi import FastAPI, HTTPException, Request, Depends
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

# rich ロギング設定
from rich.logging import RichHandler
from rich.console import Console
import logging

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True)]
)
logger = logging.getLogger(__name__)

# 設定クラス
class Settings(BaseSettings):
    redis_url: str = "redis://redis:6379"
    rate_limit_requests: int = 5
    rate_limit_window: int = 60
    max_concurrent_downloads: int = 10
    max_download_timeout: int = 3600  # 1時間
    log_level: str = "INFO"
    yt_dlp_js_runtime: Optional[str] = None  # 自動検出
    enable_ssrf_protection: bool = True
    
    class Config:
        env_file = ".env"

settings = Settings()
logging.getLogger().setLevel(getattr(logging, settings.log_level))

app = FastAPI(
    title="yt-dlp Streaming API with Deno",
    description="本番環境対応 yt-dlp ストリーミングAPI",
    version="4.0.0"
)

# CORS設定（必要に応じて）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis接続
try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("redis not installed - rate limiting disabled")

SUPPORTED_SITES: List[str] = []
redis_client: Optional[aioredis.Redis] = None
DENO_VERSION: str = "unknown"
YTDLP_VERSION: str = "unknown"
JS_RUNTIME_PATH: Optional[str] = None

class VideoRequest(BaseModel):
    url: HttpUrl = Field(..., description="動画のURL")
    format: Optional[str] = Field(None, description="カスタムフォーマット指定")
    audio_only: Optional[bool] = Field(False, description="音声のみダウンロード")
    audio_format: Optional[str] = Field("opus", description="音声フォーマット (opus/m4a/mp3)")
    quality: Optional[int] = Field(None, description="画質指定", ge=144, le=2160)
    
    @validator('url')
    def validate_url_safety(cls, v):
        """SSRF対策: 内部IPへのアクセスを防ぐ"""
        if not settings.enable_ssrf_protection:
            return v
        
        try:
            parsed = urlparse(str(v))
            hostname = parsed.hostname
            
            if not hostname:
                raise ValueError("Invalid URL")
            
            # IPアドレスの場合は直接チェック
            try:
                ip = ipaddress.ip_address(hostname)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    raise ValueError("Access to private/internal IPs is forbidden")
            except ValueError:
                # ホスト名の場合はDNS解決してチェック
                try:
                    resolved_ip = socket.gethostbyname(hostname)
                    ip = ipaddress.ip_address(resolved_ip)
                    if ip.is_private or ip.is_loopback or ip.is_link_local:
                        raise ValueError("URL resolves to private/internal IP")
                except socket.gaierror:
                    pass  # DNS解決失敗は許容（外部サービスが処理）
            
            return v
        except Exception as e:
            logger.error(f"URL validation failed: {str(e)}")
            raise ValueError(f"Invalid or unsafe URL: {str(e)}")

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
    """ファイル名を安全な形式に変換"""
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
    """ログ用に安全なURL（トークン等を削除）"""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except:
        return "invalid_url"

class RedisRateLimiter:
    """Redisベースのレート制限（Lua script使用）"""
    
    def __init__(self):
        self.max_requests = settings.rate_limit_requests
        self.window_seconds = settings.rate_limit_window
        
        # Lua script for atomic increment with expiry
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
        
        # キーを IP + endpoint + UA で構成
        key = f"rate:{client_ip}:{endpoint}:{user_agent_hash % 10000}"
        
        try:
            # Lua scriptを実行
            result = await redis_client.eval(
                self.lua_script,
                1,
                key,
                self.max_requests,
                self.window_seconds
            )
            
            allowed, ttl = result
            
            if not allowed:
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit exceeded. Retry after {ttl} seconds.",
                    headers={"Retry-After": str(ttl)}
                )
            
            return True
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Redis rate limit error: {str(e)}")
            return True

class ConcurrencyLimiter:
    """同時ダウンロード数制御"""
    
    async def __call__(self, request: Request):
        if not REDIS_AVAILABLE or redis_client is None:
            return True
        
        key = "active_downloads"
        
        try:
            current = await redis_client.incr(key)
            
            if current > settings.max_concurrent_downloads:
                await redis_client.decr(key)
                raise HTTPException(
                    status_code=503,
                    detail=f"Server busy. Max concurrent downloads: {settings.max_concurrent_downloads}"
                )
            
            # リクエストが終了したらデクリメント
            request.state.download_slot_acquired = True
            
            return True
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Concurrency limiter error: {str(e)}")
            return True

rate_limiter = RedisRateLimiter()
concurrency_limiter = ConcurrencyLimiter()

async def release_download_slot(request: Request):
    """ダウンロードスロットを解放"""
    if hasattr(request.state, 'download_slot_acquired') and request.state.download_slot_acquired:
        if redis_client:
            try:
                await redis_client.decr("active_downloads")
            except Exception as e:
                logger.error(f"Failed to release download slot: {str(e)}")

async def detect_js_runtime() -> Optional[str]:
    """JSランタイムを自動検出"""
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
                    logger.info(f"Detected JS runtime: {runtime_name} at {runtime_path}")
                    return f"{runtime_name}:{runtime_path}"
            except:
                pass
    
    return None

async def check_deno_installation() -> Dict[str, str]:
    """Denoのインストール状態を確認"""
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
            "status": "installed",
            "version": deno_version,
            "path": "/usr/local/bin/deno"
        }
    except Exception as e:
        return {
            "status": "not_found",
            "error": str(e)
        }

async def load_supported_sites():
    """サポートサイトをバックグラウンドで読み込み"""
    global SUPPORTED_SITES
    
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--list-extractors',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15.0)
        SUPPORTED_SITES = stdout.decode().strip().split('\n')
        console.print(f"[green]Cached {len(SUPPORTED_SITES)} supported sites[/green]")
        logger.info(f"Loaded {len(SUPPORTED_SITES)} supported extractors")
    except Exception as e:
        console.print("[yellow]Failed to load supported sites (non-critical)[/yellow]")
        logger.warning(f"Extractor list loading failed: {str(e)}")

@app.on_event("startup")
async def startup_event():
    """起動時処理"""
    global redis_client, DENO_VERSION, YTDLP_VERSION, JS_RUNTIME_PATH
    
    console.rule("[bold blue]Starting yt-dlp API v4.0[/bold blue]")
    
    # JSランタイム自動検出
    if settings.yt_dlp_js_runtime:
        JS_RUNTIME_PATH = settings.yt_dlp_js_runtime
        console.print(f"[cyan]Using configured JS runtime: {JS_RUNTIME_PATH}[/cyan]")
    else:
        JS_RUNTIME_PATH = await detect_js_runtime()
        if JS_RUNTIME_PATH:
            console.print(f"[green]Auto-detected JS runtime: {JS_RUNTIME_PATH}[/green]")
        else:
            console.print("[yellow]No JS runtime detected (YouTube may fail)[/yellow]")
    
    # Denoバージョン確認
    deno_info = await check_deno_installation()
    if deno_info["status"] == "installed":
        DENO_VERSION = deno_info["version"]
        console.print(f"[green]Deno {DENO_VERSION} detected[/green]")
    
    # yt-dlpバージョン確認
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--version',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        YTDLP_VERSION = stdout.decode().strip()
        console.print(f"[green]yt-dlp {YTDLP_VERSION} detected[/green]")
    except Exception as e:
        console.print("[red]yt-dlp version check failed[/red]")
        logger.error(f"yt-dlp version check failed: {str(e)}")
    
    # Redis接続
    if REDIS_AVAILABLE:
        try:
            redis_client = await aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5
            )
            await redis_client.ping()
            console.print(f"[green]Redis connected[/green]")
            logger.info("Redis connection successful")
        except Exception as e:
            console.print(f"[yellow]Redis unavailable: {str(e)}[/yellow]")
            logger.warning(f"Redis connection failed: {str(e)}")
            redis_client = None
    
    # サポートサイトをバックグラウンドで読み込み
    asyncio.create_task(load_supported_sites())
    
    console.rule("[bold green]API Ready[/bold green]")
    console.print(f"[cyan]Max concurrent downloads: {settings.max_concurrent_downloads}[/cyan]")
    console.print(f"[cyan]Download timeout: {settings.max_download_timeout}s[/cyan]")
    console.print(f"[cyan]SSRF protection: {settings.enable_ssrf_protection}[/cyan]")

@app.on_event("shutdown")
async def shutdown_event():
    """シャットダウン処理"""
    console.rule("[bold yellow]Shutting down[/bold yellow]")
    
    if redis_client:
        await redis_client.close()
        logger.info("Redis connection closed")

@app.get("/", tags=["Health"])
async def root():
    return {
        "status": "running",
        "service": "yt-dlp Streaming API",
        "version": "4.0.0",
        "deno_version": DENO_VERSION,
        "ytdlp_version": YTDLP_VERSION,
        "redis_enabled": redis_client is not None,
        "max_concurrent_downloads": settings.max_concurrent_downloads
    }

@app.get("/health", tags=["Health"])
async def health_check():
    """軽量ヘルスチェック（Kubernetes向け）"""
    redis_status = "disabled"
    if redis_client:
        try:
            await redis_client.ping()
            redis_status = "connected"
        except:
            redis_status = "disconnected"
    
    return {
        "status": "healthy",
        "redis": redis_status
    }

@app.get("/health/full", tags=["Health"])
async def health_check_full():
    """詳細ヘルスチェック"""
    
    # yt-dlpチェック
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--version',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        ytdlp_version = stdout.decode().strip()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"yt-dlp unavailable: {str(e)}")
    
    # Denoチェック
    deno_info = await check_deno_installation()
    
    # Redisチェック
    redis_status = "disabled"
    active_downloads = 0
    if redis_client:
        try:
            await redis_client.ping()
            redis_status = "connected"
            active_downloads = int(await redis_client.get("active_downloads") or 0)
        except:
            redis_status = "disconnected"
    
    health_data = {
        "status": "healthy",
        "ytdlp_version": ytdlp_version,
        "deno": deno_info,
        "redis_status": redis_status,
        "js_runtime": JS_RUNTIME_PATH,
        "active_downloads": active_downloads,
        "max_downloads": settings.max_concurrent_downloads,
        "supported_sites_loaded": len(SUPPORTED_SITES)
    }
    
    if deno_info["status"] != "installed":
        health_data["warning"] = "Deno not installed. YouTube downloads may fail."
    
    return health_data

@app.post("/info", response_model=VideoInfo, tags=["Video Info"], dependencies=[Depends(rate_limiter)])
async def get_video_info(request: VideoRequest):
    """動画情報取得"""
    
    cmd = [
        'yt-dlp',
        '--dump-json',
        '--no-playlist',
        '--match-filter', '!is_live',  # ライブ配信を除外
        '--socket-timeout', '10',
        '--retries', '3',
    ]
    
    if JS_RUNTIME_PATH:
        cmd.extend(['--js-runtimes', JS_RUNTIME_PATH])
    
    cmd.append(str(request.url))
    
    safe_url = safe_url_for_log(str(request.url))
    logger.info(f"Fetching video info: {safe_url}")
    
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
            logger.error("Video info fetch timeout")
            raise HTTPException(status_code=504, detail="Request timeout")
        
        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error(f"yt-dlp error (code {process.returncode}): {error_msg}")
            raise HTTPException(status_code=400, detail=f"Failed to fetch video info: {error_msg[:200]}")
        
        info = json.loads(stdout.decode())
        
        # ライブ配信チェック
        is_live = info.get('is_live', False)
        if is_live:
            raise HTTPException(status_code=400, detail="Live streams are not supported")
        
        logger.info(f"Video info retrieved: {info.get('title', 'Unknown')}")
        
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
            webpage_url=info.get("webpage_url", str(request.url)),
            is_live=is_live
        )
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to parse video info")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Video info error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/stream", tags=["Download"], dependencies=[Depends(rate_limiter), Depends(concurrency_limiter)])
async def stream_video(request: Request, video_request: VideoRequest):
    """ストリーミングダウンロード（改善版 - 二重アクセス解消）"""
    
    # フォーマット構築
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
        else:  # opus (default)
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
    
    # ファイル名を --print で取得（軽量・単発）
    filename = f"video.{ext}"  # デフォルト
    
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
            # 拡張子を調整
            title = os.path.splitext(raw_filename)[0]
            filename = f"{sanitize_filename(title)}.{ext}"
    except Exception as e:
        logger.warning(f"Failed to get filename: {str(e)} - using default")
    
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
    
    # DEBUGモードでは詳細ログ
    if settings.log_level != "DEBUG":
        cmd.extend(['--quiet', '--no-warnings'])
    
    if video_request.audio_only and video_request.audio_format == "mp3":
        cmd.extend(['-x', '--audio-format', 'mp3'])
    
    safe_url = safe_url_for_log(str(video_request.url))
    logger.info(f"Starting stream: {safe_url} (format: {format_str})")
    
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
            """stderrドレイン（エラー時に出力）"""
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
            """ストリーム生成"""
            CHUNK_SIZE = 1024 * 1024
            try:
                while True:
                    chunk = await process.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            except asyncio.CancelledError:
                logger.warning(f"Client disconnected: PID={process.pid}")
                process.kill()
                await process.wait()
                raise
            except Exception as e:
                logger.error(f"Stream error: {str(e)}")
                process.kill()
                await process.wait()
                raise
            finally:
                # プロセス終了待機（タイムアウト付き）
                try:
                    returncode = await asyncio.wait_for(process.wait(), timeout=5.0)
                    
                    if returncode != 0:
                        # エラーの場合のみstderrを出力
                        error_summary = '\n'.join(stderr_lines[-10:])  # 最後の10行
                        logger.error(f"yt-dlp exited with code {returncode}:\n{error_summary}")
                    
                except asyncio.TimeoutError:
                    logger.warning("Process did not terminate, killing forcefully")
                    process.kill()
                    await process.wait()
                
                # stderrタスクをクリーンアップ
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
                
                # スロット解放
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
        # 例外時もスロット解放
        await release_download_slot(request)
        
        # プロセスが生きていれば強制終了
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        
        logger.error(f"Stream error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/supported-sites", tags=["Info"])
async def get_supported_sites(
    limit: int = 50,
    search: Optional[str] = None
):
    """サポートサイト一覧（検索対応）"""
    
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
    """エラーハンドラー"""
    
    # スロット解放
    await release_download_slot(request)
    
    safe_url = safe_url_for_log(str(request.url))
    logger.error(f"HTTP {exc.status_code}: {exc.detail} - {safe_url}")
    
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
    """予期しないエラー"""
    
    # スロット解放
    await release_download_slot(request)
    
    logger.exception(f"Unhandled exception: {str(exc)}")
    
    return JSONResponse(
        status_code=500,
        content={
            "error": True,
            "status_code": 500,
            "detail": "Internal server error",
            "timestamp": datetime.now().isoformat()
        }
    )
