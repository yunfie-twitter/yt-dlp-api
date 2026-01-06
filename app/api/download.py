import asyncio
import os
import time
import uuid
import aiofiles
import json
from typing import AsyncIterator, Optional, Dict
from collections import deque
from contextlib import suppress
from urllib.parse import quote
from fastapi import APIRouter, Request, Depends, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
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
DOWNLOAD_TIMEOUT = 3600  # 1 hour max per download

# Ensure temp dir exists
os.makedirs(TEMP_DIR, exist_ok=True)

router = APIRouter()

# Global dictionary to track download progress and process
download_tasks: Dict[str, dict] = {}
# WebSocket connections per task
websocket_connections: Dict[str, list] = {}

class DownloadService:
    """
    Video download service with progress tracking and aria2c support
    """
    
    @staticmethod
    async def download_with_progress(intent: DownloadIntent, locale: str, request: Request, task_id: str) -> tuple[str, str, int]:
        """
        Download to temp file with WebSocket progress updates.
        Uses aria2c for faster multi-connection downloads.
        Returns (file_path, filename, file_size)
        """
        _ = functools.partial(i18n.get, locale=locale)
        safe_url = safe_url_for_log(intent.url)
        
        # Update status: fetching info
        download_tasks[task_id]['status'] = 'fetching_info'
        await DownloadService.broadcast_progress(task_id, {
            'status': 'fetching_info',
            'progress': 5,
            'message': '動画情報を取得中... (0/3)'
        })
        
        # 1. Determine Format String
        try:
            log_info(request, f"Determining format for {safe_url}")
            format_str = FormatDecision.decide(intent)
            log_info(request, f"Format decided: {format_str} for {safe_url}")
            
            await DownloadService.broadcast_progress(task_id, {
                'status': 'fetching_info',
                'progress': 15,
                'message': f'フォーマット決定: {format_str} (1/3)'
            })
        except Exception as e:
            log_error(request, f"Format decision error: {str(e)}")
            raise Exception(f"フォーマット決定エラー: {str(e)}")
        
        # 2. Get Filename (metadata)
        await DownloadService.broadcast_progress(task_id, {
            'status': 'fetching_info',
            'progress': 25,
            'message': 'ファイル名を取得中... (2/3)'
        })
        
        filename_cmd = YTDLPCommandBuilder.build_filename_command(intent.url, format_str)
        try:
            log_info(request, f"Fetching filename for {safe_url}")
            result = await asyncio.wait_for(
                SubprocessExecutor.run(filename_cmd, timeout=15.0),
                timeout=20.0
            )
            if result.returncode == 0:
                raw_filename = result.stdout.decode().strip()
                filename = sanitize_filename(raw_filename)
                
                # Apply file format if specified
                if intent.file_format:
                    root, _ = os.path.splitext(filename)
                    filename = f"{root}.{intent.file_format}"
                elif intent.audio_only and not filename.endswith('.mp3'):
                    root, _ = os.path.splitext(filename)
                    filename = f"{root}.mp3"
                    
                log_info(request, f"Filename resolved: {filename}")
            else:
                raise Exception("Filename retrieval failed")
        except asyncio.TimeoutError:
            log_error(request, f"Filename fetch timeout for {safe_url}")
            video_id = hash_stable(intent.url)[:8]
            ext = intent.file_format if intent.file_format else ('mp3' if intent.audio_only else 'mp4')
            filename = f"video_{video_id}.{ext}"
        except Exception as e:
            log_error(request, f"Filename error: {str(e)}")
            video_id = hash_stable(intent.url)[:8]
            ext = intent.file_format if intent.file_format else ('mp3' if intent.audio_only else 'mp4')
            filename = f"video_{video_id}.{ext}"
            log_info(request, f"Fallback filename: {filename}")

        await DownloadService.broadcast_progress(task_id, {
            'status': 'fetching_info',
            'progress': 35,
            'filename': filename,
            'message': f'ファイル名: {filename} (3/3)'
        })

        # 3. Prepare Temp File
        temp_id = str(uuid.uuid4())
        temp_path_template = os.path.join(TEMP_DIR, f"{temp_id}.%(ext)s")
        
        # 4. Build Download Command with aria2c
        cmd = YTDLPCommandBuilder.build_stream_command(
            intent.url,
            format_str,
            intent.audio_only,
            intent.file_format,
            use_aria2c=True  # Enable aria2c for faster downloads
        )
        
        try:
            o_index = cmd.index('-o')
            cmd[o_index+1] = temp_path_template
        except ValueError:
            cmd.extend(['-o', temp_path_template])
        
        # Add progress flags
        cmd = [c for c in cmd if c != '--no-progress']
        cmd.extend(['--newline', '--progress'])
        
        log_info(request, f"Starting download with aria2c to temp: {temp_path_template}")
        
        download_tasks[task_id]['status'] = 'downloading'
        download_tasks[task_id]['filename'] = filename
        
        await DownloadService.broadcast_progress(task_id, {
            'status': 'downloading',
            'progress': 40,
            'filename': filename,
            'message': 'ダウンロード中... (aria2c: 16接続)'
        })
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        # Store process for cancellation
        download_tasks[task_id]['process'] = process
        
        stderr_lines = deque(maxlen=STDERR_MAX_LINES)
        last_progress_time = time.time()
        
        async def parse_progress():
            """
            Parse yt-dlp progress and broadcast via WebSocket
            """
            nonlocal last_progress_time
            try:
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    
                    # Parse: [download]  45.2% of 10.50MiB at 2.30MiB/s ETA 00:03
                    if '[download]' in decoded:
                        progress_data = {'status': 'downloading'}
                        
                        # Extract percentage
                        percent_match = re.search(r'(\d+\.\d+)%', decoded)
                        if percent_match:
                            percentage = float(percent_match.group(1))
                            # Scale from 40-95% (40% was info fetching)
                            scaled_percentage = 40 + (percentage * 0.55)
                            progress_data['progress'] = round(scaled_percentage, 1)
                            download_tasks[task_id]['progress'] = scaled_percentage
                        
                        # Extract size
                        size_match = re.search(r'of\s+([\d.]+[KMG]iB)', decoded)
                        if size_match:
                            progress_data['total_size'] = size_match.group(1)
                        
                        # Extract speed
                        speed_match = re.search(r'at\s+([\d.]+[KMG]iB/s)', decoded)
                        if speed_match:
                            speed = speed_match.group(1)
                            progress_data['speed'] = speed
                            download_tasks[task_id]['speed'] = speed
                        
                        # Extract ETA
                        eta_match = re.search(r'ETA\s+(\d{2}:\d{2})', decoded)
                        if eta_match:
                            eta = eta_match.group(1)
                            progress_data['eta'] = eta
                            download_tasks[task_id]['eta'] = eta
                        
                        # Throttle broadcasts (max once per 0.5 seconds)
                        current_time = time.time()
                        if current_time - last_progress_time >= 0.5:
                            await DownloadService.broadcast_progress(task_id, progress_data)
                            last_progress_time = current_time
                            
                    # Check for "Destination" line (processing)
                    elif 'Destination:' in decoded or 'Merging' in decoded:
                        await DownloadService.broadcast_progress(task_id, {
                            'status': 'processing',
                            'progress': 95,
                            'message': 'ファイルを処理中...'
                        })
                        
            except Exception as e:
                log_error(request, f"Progress parsing error: {str(e)}")
        
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
        
        try:
            # Wait with timeout
            returncode = await asyncio.wait_for(process.wait(), timeout=DOWNLOAD_TIMEOUT)
            
            # Check if cancelled
            if download_tasks[task_id].get('cancelled', False):
                raise Exception("ダウンロードがキャンセルされました")
            
            if returncode != 0:
                error_summary = '\n'.join(stderr_lines)
                download_tasks[task_id]['status'] = 'error'
                download_tasks[task_id]['error'] = error_summary[:200]
                await DownloadService.broadcast_progress(task_id, {
                    'status': 'error',
                    'message': f'エラー: {error_summary[:100]}',
                    'error': error_summary[:200]
                })
                raise Exception(f"yt-dlp failed: {error_summary[:200]}")
            
            download_tasks[task_id]['progress'] = 100.0
            download_tasks[task_id]['status'] = 'completed'
            
            await DownloadService.broadcast_progress(task_id, {
                'status': 'completed',
                'progress': 100,
                'filename': filename,
                'message': 'ダウンロード完了！'
            })
                
        except asyncio.TimeoutError:
            log_error(request, f"Download timeout after {DOWNLOAD_TIMEOUT}s")
            download_tasks[task_id]['status'] = 'error'
            download_tasks[task_id]['error'] = 'Download timeout'
            process.kill()
            await process.wait()
            raise Exception("ダウンロードがタイムアウトしました")
        except Exception as e:
            log_error(request, f"Process error: {str(e)}")
            download_tasks[task_id]['status'] = 'error'
            download_tasks[task_id]['error'] = str(e)
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        finally:
             progress_task.cancel()
             stderr_task.cancel()
             with suppress(asyncio.CancelledError):
                await progress_task
                await stderr_task

        # 5. Find the generated file
        found_files = [f for f in os.listdir(TEMP_DIR) if f.startswith(temp_id)]
        if not found_files:
            raise HTTPException(status_code=500, detail="ダウンロード完了後にファイルが見つかりません")
        
        final_temp_path = os.path.join(TEMP_DIR, found_files[0])
        file_size = os.path.getsize(final_temp_path)
        
        download_tasks[task_id]['file_path'] = final_temp_path
        download_tasks[task_id]['file_size'] = file_size
        
        log_info(request, f"Download finished: {file_size / 1024 / 1024:.1f} MB")
        
        return final_temp_path, filename, file_size
    
    @staticmethod
    async def broadcast_progress(task_id: str, data: dict):
        """
        Broadcast progress to all connected WebSocket clients
        """
        if task_id not in websocket_connections:
            return
        
        # Add task_id to data
        data['task_id'] = task_id
        message = json.dumps(data)
        
        # Send to all connected clients
        disconnected = []
        for ws in websocket_connections[task_id]:
            try:
                await ws.send_text(message)
            except:
                disconnected.append(ws)
        
        # Remove disconnected clients
        for ws in disconnected:
            websocket_connections[task_id].remove(ws)

@router.post("/download/start", dependencies=[Depends(rate_limiter)])
async def start_download(request: Request, video_request: VideoRequest, background_tasks: BackgroundTasks):
    """
    Start download task and return task ID for progress tracking
    """
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)
    
    validation_result = await SecurityValidator.validate_url(str(video_request.url))
    if validation_result == UrlValidationResult.BLOCKED:
        raise HTTPException(status_code=403, detail=_("error.private_ip"))
    if validation_result == UrlValidationResult.INVALID:
        raise HTTPException(status_code=400, detail=_("error.invalid_url", reason="Invalid format"))
    
    # Generate task ID
    task_id = str(uuid.uuid4())
    
    # Initialize task tracking BEFORE starting background task
    download_tasks[task_id] = {
        'status': 'queued',
        'progress': 0.0,
        'speed': None,
        'eta': None,
        'filename': None,
        'file_path': None,
        'file_size': None,
        'error': None,
        'process': None,
        'cancelled': False,
        'created_at': time.time()
    }
    
    log_info(request, f"Created download task: {task_id}")
    
    # Start background download
    async def background_download():
        try:
            intent = video_request.to_intent()
            await DownloadService.download_with_progress(intent, locale, request, task_id)
        except Exception as e:
            log_error(request, f"Download error for task {task_id}: {str(e)}")
            if task_id in download_tasks:
                download_tasks[task_id]['status'] = 'error'
                download_tasks[task_id]['error'] = str(e)
                await DownloadService.broadcast_progress(task_id, {
                    'status': 'error',
                    'message': str(e),
                    'error': str(e)
                })
    
    background_tasks.add_task(background_download)
    
    return JSONResponse({
        'task_id': task_id,
        'status': 'queued',
        'message': 'Download task created'
    })

@router.post("/download/cancel/{task_id}")
async def cancel_download(task_id: str):
    """
    Cancel an ongoing download
    """
    
    if task_id not in download_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task_info = download_tasks[task_id]
    
    # Mark as cancelled
    task_info['cancelled'] = True
    
    # Kill the process if it exists
    if 'process' in task_info and task_info['process']:
        process = task_info['process']
        try:
            process.kill()
            await process.wait()
        except:
            pass
    
    # Update status
    task_info['status'] = 'cancelled'
    
    # Broadcast cancellation
    await DownloadService.broadcast_progress(task_id, {
        'status': 'cancelled',
        'message': 'ダウンロードがキャンセルされました'
    })
    
    # Cleanup temp files
    if task_info.get('file_path') and os.path.exists(task_info['file_path']):
        with suppress(OSError):
            os.remove(task_info['file_path'])
    
    return JSONResponse({
        'task_id': task_id,
        'status': 'cancelled',
        'message': 'Download cancelled successfully'
    })

@router.websocket("/download/progress/ws/{task_id}")
async def websocket_progress(websocket: WebSocket, task_id: str):
    """
    WebSocket endpoint for real-time progress updates
    """
    await websocket.accept()
    
    # Register WebSocket connection
    if task_id not in websocket_connections:
        websocket_connections[task_id] = []
    websocket_connections[task_id].append(websocket)
    
    try:
        # Send initial state if task exists
        if task_id in download_tasks:
            task_info = download_tasks[task_id]
            await websocket.send_text(json.dumps({
                'task_id': task_id,
                'status': task_info['status'],
                'progress': task_info['progress'],
                'filename': task_info['filename'],
                'message': '接続済み'
            }))
        else:
            # Task not found - might not be created yet, wait a bit
            await asyncio.sleep(0.5)
            if task_id in download_tasks:
                task_info = download_tasks[task_id]
                await websocket.send_text(json.dumps({
                    'task_id': task_id,
                    'status': task_info['status'],
                    'progress': task_info['progress'],
                    'filename': task_info['filename']
                }))
        
        # Keep connection alive
        while True:
            try:
                # Receive ping/pong to keep connection alive
                data = await websocket.receive_text()
                if data == 'ping':
                    await websocket.send_text('pong')
            except WebSocketDisconnect:
                break
            except Exception:
                break
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        # Unregister connection
        if task_id in websocket_connections:
            if websocket in websocket_connections[task_id]:
                websocket_connections[task_id].remove(websocket)
            if not websocket_connections[task_id]:
                del websocket_connections[task_id]
        # DO NOT delete task info here - keep it until file is downloaded

@router.get("/download/file/{task_id}")
async def download_file(task_id: str):
    """
    Download the completed file (browser native download)
    """
    
    if task_id not in download_tasks:
        raise HTTPException(status_code=404, detail="タスクが見つかりません")
    
    task_info = download_tasks[task_id]
    
    if task_info['status'] != 'completed':
        raise HTTPException(status_code=400, detail=f"ダウンロードが完了していません。ステータス: {task_info['status']}")
    
    file_path = task_info['file_path']
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")
    
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
            # Close any remaining WebSocket connections
            if task_id in websocket_connections:
                for ws in websocket_connections[task_id]:
                    with suppress(Exception):
                        await ws.close()
                del websocket_connections[task_id]
    
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

# Cleanup old tasks periodically
@router.on_event("startup")
async def cleanup_old_tasks():
    """
    Periodic cleanup of old/stale tasks
    """
    async def cleanup():
        while True:
            await asyncio.sleep(600)  # Every 10 minutes
            current_time = time.time()
            to_delete = []
            
            for task_id, task_info in download_tasks.items():
                # Remove tasks older than 1 hour
                if current_time - task_info.get('created_at', 0) > 3600:
                    to_delete.append(task_id)
                    # Cleanup file if exists
                    if task_info.get('file_path') and os.path.exists(task_info['file_path']):
                        with suppress(OSError):
                            os.remove(task_info['file_path'])
            
            for task_id in to_delete:
                del download_tasks[task_id]
                if task_id in websocket_connections:
                    del websocket_connections[task_id]
    
    asyncio.create_task(cleanup())

# Keep original /download endpoint for backward compatibility
@router.post("/download", dependencies=[Depends(rate_limiter), Depends(concurrency_limiter)])
async def download_video_legacy(request: Request, video_request: VideoRequest):
    """
    Legacy direct download (immediate streaming)
    """
    
    locale = get_locale(request.headers.get("accept-language"))
    _ = functools.partial(i18n.get, locale=locale)
    
    validation_result = await SecurityValidator.validate_url(str(video_request.url))
    if validation_result == UrlValidationResult.BLOCKED:
        await release_download_slot(request)
        raise HTTPException(status_code=403, detail=_("error.private_ip"))
    if validation_result == UrlValidationResult.INVALID:
        await release_download_slot(request)
        raise HTTPException(status_code=400, detail=_("error.invalid_url", reason="Invalid format"))
    
    # Use new progress-enabled download
    task_id = str(uuid.uuid4())
    download_tasks[task_id] = {
        'status': 'downloading',
        'progress': 0.0,
        'created_at': time.time()
    }
    
    try:
        intent = video_request.to_intent()
        file_path, filename, file_size = await DownloadService.download_with_progress(intent, locale, request, task_id)
        
        async def generate():
            try:
                async with aiofiles.open(file_path, 'rb') as f:
                    while True:
                        chunk = await f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        yield chunk
            finally:
                with suppress(OSError):
                    os.remove(file_path)
                if task_id in download_tasks:
                    del download_tasks[task_id]
                await release_download_slot(request)
        
        encoded_filename = quote(filename)
        headers = {
            'Content-Disposition': f"attachment; filename*=UTF-8''{encoded_filename}",
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'no-cache',
            'Content-Length': str(file_size)
        }
        
        return StreamingResponse(
            generate(),
            media_type='application/octet-stream',
            headers=headers
        )
        
    except Exception as e:
        await release_download_slot(request)
        if task_id in download_tasks:
            del download_tasks[task_id]
        raise HTTPException(status_code=500, detail=str(e))
