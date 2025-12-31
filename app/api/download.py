import asyncio
import os
import time
import uuid
import aiofiles
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
TEMP_DIR = "/tmp/ytdlp_downloads"

# Ensure temp dir exists
os.makedirs(TEMP_DIR, exist_ok=True)

router = APIRouter()

class DownloadService:
    """Video download service"""
    
    @staticmethod
    async def download(intent: DownloadIntent, locale: str, request: Request) -> tuple[AsyncIterator[bytes], dict, Optional[int]]:
        """
        Download to temp file then stream.
        Reliable for merges/conversions.
        Returns (generator, headers, content_length)
        """
        _ = functools.partial(i18n.get, locale=locale)
        safe_url = safe_url_for_log(intent.url)
        
        # 1. Determine Format String
        format_str = intent.custom_format
        if intent.custom_format and intent.audio_format and not intent.audio_only:
             format_str = f"{intent.custom_format}+{intent.audio_format}"
        elif not format_str and intent.audio_format:
            format_str = intent.audio_format
        if not format_str:
            format_str = FormatDecision.decide(intent)
            
        log_info(request, f"Format decided: {format_str} for {safe_url}")
        
        # 2. Get Filename (metadata)
        filename_cmd = YTDLPCommandBuilder.build_filename_command(intent.url, format_str)
        try:
            log_info(request, f"Fetching filename for {safe_url}")
            result = await SubprocessExecutor.run(filename_cmd, timeout=15.0)
            if result.returncode == 0:
                raw_filename = result.stdout.decode().strip()
                filename = sanitize_filename(raw_filename)
                
                if intent.file_format:
                    root, _ = os.path.splitext(filename)
                    filename = f"{root}.{intent.file_format}"
                log_info(request, f"Filename resolved: {filename}")
            else:
                raise Exception("Filename retrieval failed")
        except Exception as e:
            log_error(request, f"Filename error: {str(e)}")
            video_id = hash_stable(intent.url)[:8]
            ext = intent.file_format if intent.file_format else ('mp3' if intent.audio_only else 'mp4')
            filename = f"video_{video_id}.{ext}"
            log_info(request, f"Fallback filename: {filename}")

        # 3. Prepare Temp File
        temp_id = str(uuid.uuid4())
        # We don't know the final extension yt-dlp will produce (it might merge to mkv then remux to mp4)
        # So we give a template. yt-dlp adds extension automatically if not in template?
        # Actually, we should specify output template to force a specific path.
        # But we need to handle extensions.
        # Safest is to use "%(title)s [%(id)s].%(ext)s" style but mapped to our temp dir?
        # Or just use a fixed temp name template.
        temp_path_template = os.path.join(TEMP_DIR, f"{temp_id}.%(ext)s")
        
        # 4. Build Download Command (Output to file)
        cmd = YTDLPCommandBuilder.build_stream_command(
            intent.url,
            format_str,
            intent.audio_only,
            intent.file_format
        )
        
        # Replace '-o -' with file output
        # Remove existing '-o' and '-'
        try:
            o_index = cmd.index('-o')
            cmd[o_index+1] = temp_path_template
        except ValueError:
            cmd.extend(['-o', temp_path_template])

        # Remove --quiet to debug? Keep it quiet but capture stderr.
        
        log_info(request, f"Starting download to temp: {temp_path_template}")
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        stderr_lines = deque(maxlen=STDERR_MAX_LINES)
        
        async def drain_stderr():
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
        
        # Wait for completion
        try:
            # Increase timeout for full download
            # TODO: make configurable or adaptive
            # Currently just waiting indefinitely (or very long)
            # But process.communicate() reads all.
            # We already have drain_stderr task.
            # Just wait for wait()
            
            # Use stdout for progress if possible?
            # Since we removed -o -, stdout might be empty or progress info.
            # But we passed --no-progress.
            
            returncode = await process.wait()
            
            if returncode != 0:
                error_summary = '\n'.join(stderr_lines)
                raise Exception(f"yt-dlp failed: {error_summary[:200]}")
                
        except Exception as e:
            log_error(request, f"Process error: {str(e)}")
            process.kill()
            await process.wait()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
             stderr_task.cancel()
             with suppress(asyncio.CancelledError):
                await stderr_task

        # 5. Find the generated file
        # yt-dlp might have added an extension.
        # We search for files starting with temp_id in TEMP_DIR
        found_files = [f for f in os.listdir(TEMP_DIR) if f.startswith(temp_id)]
        if not found_files:
            raise HTTPException(status_code=500, detail="Output file not found after download")
        
        final_temp_path = os.path.join(TEMP_DIR, found_files[0])
        file_size = os.path.getsize(final_temp_path)
        
        log_info(request, f"Download finished. Streaming {file_size / 1024 / 1024:.1f} MB")

        # 6. Stream Generator
        async def generate():
            try:
                async with aiofiles.open(final_temp_path, 'rb') as f:
                    while True:
                        chunk = await f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        yield chunk
            except Exception as e:
                log_error(request, f"Streaming error: {str(e)}")
            finally:
                # Cleanup
                with suppress(OSError):
                    os.remove(final_temp_path)
                log_info(request, f"Cleaned up {final_temp_path}")

        encoded_filename = quote(filename)
        headers = {
            'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}",
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'no-cache',
            'Accept-Ranges': 'bytes',
            'Content-Length': str(file_size)
        }
        
        return generate(), headers, file_size

@router.post("/download", dependencies=[Depends(rate_limiter), Depends(concurrency_limiter)])
async def download_video(request: Request, video_request: VideoRequest):
    """Download video directly"""
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)
    
    validation_result = await SecurityValidator.validate_url(str(video_request.url))
    if validation_result == UrlValidationResult.BLOCKED:
        await release_download_slot(request)
        raise HTTPException(status_code=403, detail=_("error.private_ip"))
    if validation_result == UrlValidationResult.INVALID:
        await release_download_slot(request)
        raise HTTPException(status_code=400, detail=_("error.invalid_url", reason="Invalid format"))
    
    intent = video_request.to_intent()
    safe_url = safe_url_for_log(intent.url)
    log_info(request, _("log.starting_stream", url=safe_url, format="download_temp_file"))
    
    try:
        generator, headers, content_length = await DownloadService.download(intent, locale, request)
        
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