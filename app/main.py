from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, HttpUrl, Field
from pydantic_settings import BaseSettings
import subprocess
import asyncio
from typing import Optional, List, Dict
import logging
import unicodedata
import re
from datetime import datetime
import json
import os

# 設定クラス
class Settings(BaseSettings):
    redis_url: str = "redis://redis:6379"
    rate_limit_requests: int = 5
    rate_limit_window: int = 60
    log_level: str = "INFO"
    
    class Config:
        env_file = ".env"

settings = Settings()

# ロギング設定
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="yt-dlp Streaming API",
    description="動画をストリーミングでダウンロードするAPI（Docker完全対応版）",
    version="3.1.0"
)

# Redis接続
try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("redis未インストール")

SUPPORTED_SITES: List[str] = []
redis_client: Optional[aioredis.Redis] = None

class VideoRequest(BaseModel):
    url: HttpUrl = Field(..., description="動画のURL")
    format: Optional[str] = Field(None, description="カスタムフォーマット指定")
    audio_only: Optional[bool] = Field(False, description="音声のみダウンロード")
    audio_format: Optional[str] = Field("bestaudio", description="音声フォーマット")
    quality: Optional[int] = Field(None, description="画質指定", ge=144, le=2160)

class VideoInfo(BaseModel):
    title: str
    duration: Optional[int]
    ext: str
    filesize: Optional[int]
    formats: List[Dict]
    thumbnail: Optional[str]
    uploader: Optional[str]
    webpage_url: str

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

class RedisRateLimiter:
    def __init__(self):
        self.max_requests = settings.rate_limit_requests
        self.window_seconds = settings.rate_limit_window
    
    async def __call__(self, request: Request):
        if not REDIS_AVAILABLE or redis_client is None:
            return True
        
        client_ip = request.client.host
        key = f"rate_limit:{client_ip}"
        
        try:
            current = await redis_client.incr(key)
            
            if current == 1:
                await redis_client.expire(key, self.window_seconds)
            
            if current > self.max_requests:
                ttl = await redis_client.ttl(key)
                raise HTTPException(
                    status_code=429,
                    detail=f"レート制限に達しました。{ttl}秒後に再試行してください。",
                    headers={"Retry-After": str(ttl)}
                )
            
            return True
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Redis エラー: {str(e)}")
            return True

rate_limiter = RedisRateLimiter()

@app.on_event("startup")
async def startup_event():
    """起動時処理"""
    global SUPPORTED_SITES, redis_client
    
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
            logger.info("Redis接続成功")
        except Exception as e:
            logger.warning(f"Redis接続失敗: {str(e)}")
            redis_client = None
    
    # サポートサイト読み込み
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--list-extractors',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10.0)
        SUPPORTED_SITES = stdout.decode().strip().split('\n')
        logger.info(f"サポートサイト {len(SUPPORTED_SITES)} 件をキャッシュ")
    except Exception as e:
        logger.error(f"サポートサイト読み込み失敗: {str(e)}")

@app.on_event("shutdown")
async def shutdown_event():
    """シャットダウン処理"""
    if redis_client:
        await redis_client.close()
        logger.info("Redis接続をクローズ")

@app.get("/", tags=["Health"])
async def root():
    return {
        "status": "running",
        "service": "yt-dlp Streaming API",
        "version": "3.1.0",
        "redis_enabled": redis_client is not None
    }

@app.get("/health", tags=["Health"])
async def health_check():
    """ヘルスチェック"""
    try:
        process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--version',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5.0)
        yt_dlp_version = stdout.decode().strip()
        
        # Redisヘルスチェック
        redis_status = "disabled"
        if redis_client:
            try:
                await redis_client.ping()
                redis_status = "connected"
            except:
                redis_status = "disconnected"
        
        return {
            "status": "healthy",
            "yt_dlp_version": yt_dlp_version,
            "redis_status": redis_status
        }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"yt-dlp unavailable: {str(e)}")

@app.post("/info", response_model=VideoInfo, tags=["Video Info"], dependencies=[Depends(rate_limiter)])
async def get_video_info(request: VideoRequest):
    """動画情報取得"""
    cmd = [
        'yt-dlp',
        '--dump-json',
        '--no-playlist',
        str(request.url)
    ]
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30.0)
        
        if process.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error(f"yt-dlp エラー: {error_msg}")
            raise HTTPException(status_code=400, detail=f"動画情報取得失敗: {error_msg}")
        
        info = json.loads(stdout.decode())
        
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
            webpage_url=info.get("webpage_url", str(request.url))
        )
        
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="タイムアウト")
    except Exception as e:
        logger.error(f"エラー: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/stream", tags=["Download"], dependencies=[Depends(rate_limiter)])
async def stream_video(request: VideoRequest):
    """ストリーミングダウンロード"""
    
    # 動画情報取得
    try:
        info_process = await asyncio.create_subprocess_exec(
            'yt-dlp', '--dump-json', '--no-playlist', str(request.url),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await asyncio.wait_for(info_process.communicate(), timeout=30.0)
        info = json.loads(stdout.decode())
        title = info.get('title', 'video')
    except:
        title = 'video'
    
    # フォーマット構築
    if request.format:
        format_str = request.format
        ext = 'mp4'
        media_type = 'video/mp4'
    elif request.audio_only:
        if request.audio_format == "mp3":
            format_str = 'bestaudio'
            ext = 'mp3'
            media_type = 'audio/mpeg'
        else:
            format_str = 'bestaudio'
            ext = 'webm'
            media_type = 'audio/webm'
    elif request.quality:
        height = request.quality
        format_str = (
            f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]/"
            f"best[ext=mp4][height<={height}]/best"
        )
        ext = 'mp4'
        media_type = 'video/mp4'
    else:
        format_str = 'best'
        ext = 'mp4'
        media_type = 'video/mp4'
    
    safe_title = sanitize_filename(title)
    filename = f"{safe_title}.{ext}"
    
    cmd = [
        'yt-dlp',
        str(request.url),
        '-f', format_str,
        '-o', '-',
        '--no-playlist',
        '--quiet',
        '--no-warnings',
    ]
    
    if request.audio_only and request.audio_format == "mp3":
        cmd.extend(['-x', '--audio-format', 'mp3'])
    
    logger.info(f"コマンド: {' '.join(cmd)}")
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        async def drain_stderr():
            """stderrドレイン"""
            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    logger.debug(f"stderr: {line.decode().strip()}")
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
                logger.warning(f"クライアント切断 PID={process.pid}")
                process.kill()
                raise
            finally:
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
        
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'X-Content-Type-Options': 'nosniff',
        }
        
        return StreamingResponse(
            generate(),
            media_type=media_type,
            headers=headers
        )
        
    except Exception as e:
        logger.error(f"エラー: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/supported-sites", tags=["Info"])
async def get_supported_sites(limit: int = 50):
    """サポートサイト一覧"""
    return {
        "count": len(SUPPORTED_SITES),
        "sites": SUPPORTED_SITES[:limit]
    }

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """エラーハンドラー"""
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
