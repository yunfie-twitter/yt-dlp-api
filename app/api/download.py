import asyncio
import os
from typing import AsyncIterator, Optional
from collections import deque
from contextlib import suppress
from urllib.parse import quote
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import StreamingResponse
from app.models.request import VideoRequest
from app.models.internal import DownloadIntent
from app.services.ytdlp import YTDLPCommandBuilder, SubprocessExecutor
from app.services.format import FormatDecision
from app.utils.hash import hash_stable
from app.utils.filename import sanitize_filename
from app.core.security import SecurityValidator, UrlValidationResult
from app.core.logging import log_info, log_error
from app.infra.rate_limit import rate_limiter
from app.infra.concurrency import concurrency_limiter, release_download_slot
from app.utils.locale import get_locale, safe_url_for_log
from app.i18n import i18n
import functools

CHUNK_SIZE = 4 * 1024 * 1024
STDERR_MAX_LINES = 50

router = APIRouter()

class DownloadService:
    """Video download service"""
    
    @staticmethod
    async def download(intent: DownloadIntent, locale: str) -> tuple[AsyncIterator[bytes], dict, Optional[int]]:
        """
        Stream video download with single yt-dlp call.
        Returns (generator, headers, content_length)
        """
        _ = functools.partial(i18n.get, locale=locale)
        
        # 1. Determine Format String
        # Priority: custom_format (video) > audio_format (audio) > default (FormatDecision)
        format_str = intent.custom_format
        
        if not format_str and intent.audio_format:
            format_str = intent.audio_format
            
        if not format_str:
            format_str = FormatDecision.decide(intent)
        
        # 2. Get Filename (metadata)
        # Get filename first (synchronous-like but async execution)
        filename_cmd = YTDLPCommandBuilder.build_filename_command(intent.url, format_str)
        try:
            # We use a short timeout for filename retrieval
            result = await SubprocessExecutor.run(filename_cmd, timeout=10.0)
            if result.returncode == 0:
                raw_filename = result.stdout.decode().strip()
                filename = sanitize_filename(raw_filename)
                
                # Force extension if file_format is specified
                if intent.file_format:
                    root, _ = os.path.splitext(filename)
                    # Clean any double extension if present
                    filename = f"{root}.{intent.file_format}"
                    
            else:
                raise Exception("Filename retrieval failed")
        except Exception:
             # Fallback on error
            video_id = hash_stable(intent.url)[:8]
            # Try to guess extension
            if intent.file_format:
                ext = intent.file_format
            elif intent.audio_only:
                ext = 'mp3' # Default fallback for audio
            else:
                ext = 'mp4' # Default fallback for video
            filename = f"video_{video_id}.{ext}"

        # 3. Build Stream Command
        # Pass file_format to enable remuxing (video) or conversion (audio)
        cmd = YTDLPCommandBuilder.build_stream_command(
            intent.url,
            format_str,
            intent.audio_only,
            intent.file_format
        )
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        # We rely on chunked transfer encoding (no Content-Length)
        content_length = None
        
        stderr_lines = deque(maxlen=STDERR_MAX_LINES)
        
        async def drain_stderr():
            """Drain stderr to prevent buffer deadlock"""
            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    if process.returncode is None or process.returncode != 0:
                        stderr_lines.append(decoded)
            except:
                pass
        
        stderr_task = asyncio.create_task(drain_stderr())
        
        async def generate():
            """Stream generator"""
            try:
                while True:
                    chunk = await process.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                raise
            except Exception as e:
                process.kill()
                await process.wait()
                raise HTTPException(status_code=500, detail=str(e))
            finally:
                try:
                    returncode = await asyncio.wait_for(process.wait(), timeout=5.0)
                    
                    if returncode != 0:
                        error_summary = '\n'.join(stderr_lines)
                        raise HTTPException(status_code=500, detail=f"Download failed: {error_summary[:200]}")
                    
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task
        
        # Use RFC 5987 encoding for non-ASCII filenames
        encoded_filename = quote(filename)
        
        headers = {
            'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}",
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'no-cache',
            'Accept-Ranges': 'none',
        }
        
        if content_length:
            headers['Content-Length'] = str(content_length)
        
        return generate(), headers, content_length

@router.post("/download", dependencies=[Depends(rate_limiter), Depends(concurrency_limiter)])
async def download_video(request: Request, video_request: VideoRequest):
    """Download video directly"""
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)
    
    # SSRF check
    validation_result = await SecurityValidator.validate_url(str(video_request.url))
    
    if validation_result == UrlValidationResult.BLOCKED:
        await release_download_slot(request)
        raise HTTPException(status_code=403, detail=_("error.private_ip"))
    
    if validation_result == UrlValidationResult.INVALID:
        await release_download_slot(request)
        raise HTTPException(status_code=400, detail=_("error.invalid_url", reason="Invalid format"))
    
    intent = video_request.to_intent()
    
    safe_url = safe_url_for_log(intent.url)
    log_info(request, _("log.starting_stream", url=safe_url, format="download"))
    
    try:
        generator, headers, content_length = await DownloadService.download(intent, locale)
        
        # Wrap generator to ensure cleanup
        async def wrapped_generator():
            try:
                async for chunk in generator:
                    yield chunk
            finally:
                await release_download_slot(request)
        
        return StreamingResponse(
            wrapped_generator(),
            media_type=headers.get('Content-Type', 'application/octet-stream'),
            headers=headers
        )
        
    except HTTPException:
        await release_download_slot(request)
        raise
    except Exception as e:
        await release_download_slot(request)
        log_error(request, f"Download error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))