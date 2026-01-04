import asyncio
import os
import time
import uuid
import aiofiles
from typing import AsyncIterator, Optional, Dict
from collections import deque
from contextlib import suppress
from urllib.parse import quote
from fastapi import APIRouter, Request, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
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
import re

CHUNK_SIZE = 4 * 1024 * 1024
STDERR_MAX_LINES = 50
TEMP_DIR = "/tmp/ytdlp_downloads"

# Ensure temp dir exists
os.makedirs(TEMP_DIR, exist_ok=True)

router = APIRouter()

# Global dictionary to track download progress
download_tasks: Dict[str, dict] = {}

class DownloadService:
    """Video download service"""
    
    @staticmethod
    async def download(intent: DownloadIntent, locale: str, request: Request, task_id: Optional[str] = None) -> tuple[AsyncIterator[bytes], dict, Optional[int]]:
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
        temp_path_template = os.path.join(TEMP_DIR, f"{temp_id}.%(ext)s")
        
        # 4. Build Download Command (Output to file)
        cmd = YTDLPCommandBuilder.build_stream_command(
            intent.url,
            format_str,
            intent.audio_only,
            intent.file_format
        )
        
        # Replace '-o -' with file output and add progress flag
        try:
            o_index = cmd.index('-o')
            cmd[o_index+1] = temp_path_template
        except ValueError:
            cmd.extend(['-o', temp_path_template])
        
        # Remove --no-progress if exists and add --newline for progress tracking
        cmd = [c for c in cmd if c != '--no-progress']
        cmd.extend(['--newline', '--progress'])
        
        log_info(request, f"Starting download to temp: {temp_path_template}")
        
        if task_id:
            download_tasks[task_id]['status'] = 'downloading'
            download_tasks[task_id]['filename'] = filename
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        stderr_lines = deque(maxlen=STDERR_MAX_LINES)
        
        async def parse_progress():
            """Parse yt-dlp progress from stdout"""
            try:
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    
                    # Parse progress line: [download]  45.2% of 10.50MiB at 2.30MiB/s ETA 00:03
                    if task_id and '[download]' in decoded:
                        # Extract percentage
                        percent_match = re.search(r'(\d+\.\d+)%', decoded)
                        if percent_match:
                            percentage = float(percent_match.group(1))
                            download_tasks[task_id]['progress'] = percentage
                        
                        # Extract speed
                        speed_match = re.search(r'at\s+([\d.]+[KMG]iB/s)', decoded)
                        if speed_match:
                            download_tasks[task_id]['speed'] = speed_match.group(1)
                        
                        # Extract ETA
                        eta_match = re.search(r'ETA\s+(\d{2}:\d{2})', decoded)
                        if eta_match:
                            download_tasks[task_id]['eta'] = eta_match.group(1)
            except:
                pass
        
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
        
        progress_task = asyncio.create_task(parse_progress())
        stderr_task = asyncio.create_task(drain_stderr())
        
        # Wait for completion
        try:
            returncode = await process.wait()
            
            if returncode != 0:
                error_summary = '\n'.join(stderr_lines)
                if task_id:
                    download_tasks[task_id]['status'] = 'error'
                    download_tasks[task_id]['error'] = error_summary[:200]
                raise Exception(f"yt-dlp failed: {error_summary[:200]}")
            
            if task_id:
                download_tasks[task_id]['progress'] = 100.0
                download_tasks[task_id]['status'] = 'completed'
                
        except Exception as e:
            log_error(request, f"Process error: {str(e)}")
            if task_id:
                download_tasks[task_id]['status'] = 'error'
                download_tasks[task_id]['error'] = str(e)
            process.kill()
            await process.wait()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
             progress_task.cancel()
             stderr_task.cancel()
             with suppress(asyncio.CancelledError):
                await progress_task
                await stderr_task

        # 5. Find the generated file
        found_files = [f for f in os.listdir(TEMP_DIR) if f.startswith(temp_id)]
        if not found_files:
            raise HTTPException(status_code=500, detail="Output file not found after download")
        
        final_temp_path = os.path.join(TEMP_DIR, found_files[0])
        file_size = os.path.getsize(final_temp_path)
        
        if task_id:
            download_tasks[task_id]['file_path'] = final_temp_path
            download_tasks[task_id]['file_size'] = file_size
        
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
                if task_id and task_id in download_tasks:
                    del download_tasks[task_id]

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
    """Download video directly (synchronous)"""
    
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

@router.post("/download/async", dependencies=[Depends(rate_limiter)])
async def download_video_async(request: Request, video_request: VideoRequest, background_tasks: BackgroundTasks):
    """Start async download and return task ID"""
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)
    
    validation_result = await SecurityValidator.validate_url(str(video_request.url))
    if validation_result == UrlValidationResult.BLOCKED:
        raise HTTPException(status_code=403, detail=_("error.private_ip"))
    if validation_result == UrlValidationResult.INVALID:
        raise HTTPException(status_code=400, detail=_("error.invalid_url", reason="Invalid format"))
    
    # Generate task ID
    task_id = str(uuid.uuid4())
    
    # Initialize task tracking
    download_tasks[task_id] = {
        'status': 'queued',
        'progress': 0.0,
        'speed': None,
        'eta': None,
        'filename': None,
        'file_path': None,
        'file_size': None,
        'error': None,
        'created_at': time.time()
    }
    
    # Start background download
    async def background_download():
        try:
            intent = video_request.to_intent()
            await DownloadService.download(intent, locale, request, task_id=task_id)
        except Exception as e:
            log_error(request, f"Async download error: {str(e)}")
            if task_id in download_tasks:
                download_tasks[task_id]['status'] = 'error'
                download_tasks[task_id]['error'] = str(e)
    
    background_tasks.add_task(background_download)
    
    return JSONResponse({
        'task_id': task_id,
        'status': 'queued',
        'message': 'Download task created'
    })

@router.get("/download/progress/{task_id}")
async def get_download_progress(task_id: str):
    """Get download progress for a task"""
    
    if task_id not in download_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task_info = download_tasks[task_id]
    
    response = {
        'task_id': task_id,
        'status': task_info['status'],
        'progress': task_info['progress'],
        'filename': task_info['filename']
    }
    
    if task_info['speed']:
        response['speed'] = task_info['speed']
    if task_info['eta']:
        response['eta'] = task_info['eta']
    if task_info['file_size']:
        response['file_size'] = task_info['file_size']
    if task_info['error']:
        response['error'] = task_info['error']
    
    return JSONResponse(response)

@router.get("/download/file/{task_id}")
async def get_download_file(task_id: str):
    """Download the completed file"""
    
    if task_id not in download_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task_info = download_tasks[task_id]
    
    if task_info['status'] != 'completed':
        raise HTTPException(status_code=400, detail=f"Download not completed. Current status: {task_info['status']}")
    
    file_path = task_info['file_path']
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    filename = task_info['filename']
    file_size = task_info['file_size']
    
    async def generate():
        try:
            async with aiofiles.open(file_path, 'rb') as f:
                while True:
                    chunk = await f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        finally:
            # Cleanup after download
            with suppress(OSError):
                os.remove(file_path)
            if task_id in download_tasks:
                del download_tasks[task_id]
    
    encoded_filename = quote(filename)
    headers = {
        'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}",
        'X-Content-Type-Options': 'nosniff',
        'Cache-Control': 'no-cache',
        'Accept-Ranges': 'bytes',
        'Content-Length': str(file_size)
    }
    
    return StreamingResponse(
        generate(),
        media_type='application/octet-stream',
        headers=headers
    )