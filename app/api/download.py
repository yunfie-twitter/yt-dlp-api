import asyncio
import os
import time
import uuid
import aiofiles
import json
from typing import AsyncIterator, Optional, Dict, List
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
from app.infra.redis import get_redis
import functools
import re

CHUNK_SIZE = 4 * 1024 * 1024
STDERR_MAX_LINES = 50
TEMP_DIR = "/tmp/ytdlp_downloads"
DOWNLOAD_TIMEOUT = 3600  # 1 hour max per download

# Ensure temp dir exists
os.makedirs(TEMP_DIR, exist_ok=True)

router = APIRouter()

# Global state for In-Memory mode
memory_download_tasks: Dict[str, dict] = {}
memory_websocket_connections: Dict[str, List[WebSocket]] = {}

# Process tracking (always local)
local_processes: Dict[str, asyncio.subprocess.Process] = {}

class TaskStateManager:
    """
    Abstracts task state management to support both Redis and In-Memory modes.
    """
    
    @staticmethod
    def _use_redis() -> bool:
        return get_redis() is not None

    @staticmethod
    def get_key(task_id: str) -> str:
        return f"task:{task_id}"

    @staticmethod
    async def save_task(task_id: str, data: dict, expire: int = 7200):
        redis = get_redis()
        if redis:
            await redis.set(TaskStateManager.get_key(task_id), json.dumps(data), ex=expire)
            await redis.sadd("active_tasks", task_id)
        else:
            # In-Memory
            memory_download_tasks[task_id] = data

    @staticmethod
    async def get_task(task_id: str) -> Optional[dict]:
        redis = get_redis()
        if redis:
            data = await redis.get(TaskStateManager.get_key(task_id))
            return json.loads(data) if data else None
        else:
            # In-Memory
            return memory_download_tasks.get(task_id)

    @staticmethod
    async def update_task(task_id: str, updates: dict):
        redis = get_redis()
        if redis:
            # Simple merge for Redis
            current = await TaskStateManager.get_task(task_id)
            if current:
                current.update(updates)
                await TaskStateManager.save_task(task_id, current)
        else:
            # In-Memory
            if task_id in memory_download_tasks:
                memory_download_tasks[task_id].update(updates)

    @staticmethod
    async def delete_task(task_id: str):
        redis = get_redis()
        if redis:
            await redis.delete(TaskStateManager.get_key(task_id))
            await redis.srem("active_tasks", task_id)
        else:
            # In-Memory
            if task_id in memory_download_tasks:
                del memory_download_tasks[task_id]
            if task_id in memory_websocket_connections:
                del memory_websocket_connections[task_id]

    @staticmethod
    async def publish_progress(task_id: str, data: dict):
        redis = get_redis()
        if redis:
            # Redis Pub/Sub
            data['task_id'] = task_id
            await redis.publish(f"task:{task_id}:progress", json.dumps(data))
        else:
            # In-Memory Broadcast
            if task_id in memory_websocket_connections:
                data['task_id'] = task_id
                message = json.dumps(data)
                disconnected = []
                for ws in memory_websocket_connections[task_id]:
                    try:
                        await ws.send_text(message)
                    except:
                        disconnected.append(ws)
                
                for ws in disconnected:
                    memory_websocket_connections[task_id].remove(ws)

    @staticmethod
    async def get_active_tasks_list() -> List[dict]:
        redis = get_redis()
        tasks = []
        if redis:
            active_ids = await redis.smembers("active_tasks")
            for task_id in active_ids:
                task_info = await TaskStateManager.get_task(task_id)
                if task_info:
                    tasks.append(task_info)
        else:
            tasks = list(memory_download_tasks.values())
        
        # Add task_id to dict if missing (it should be there usually)
        # and sort
        tasks.sort(key=lambda x: x.get('created_at', 0), reverse=True)
        return tasks

class DownloadService:
    """
    Video download service with progress tracking and aria2c support
    """
    
    @staticmethod
    async def download_with_progress(intent: DownloadIntent, locale: str, request: Request, task_id: str) -> tuple[str, str, int]:
        """
        Download to temp file with WebSocket progress updates via Redis Pub/Sub or In-Memory broadcast.
        """
        _ = functools.partial(i18n.get, locale=locale)
        safe_url = safe_url_for_log(intent.url)
        
        # Log: Download started
        log_info(request, f"[DOWNLOAD START] Task: {task_id} | URL: {safe_url}")
        
        # Update status: fetching_info
        await TaskStateManager.update_task(task_id, {
            'status': 'fetching_info',
            'url': intent.url
        })
        await TaskStateManager.publish_progress(task_id, {
            'status': 'fetching_info',
            'progress': 5,
            'message': '動画情報を取得中... (0/3)'
        })
        
        # 1. Determine Format String
        try:
            log_info(request, f"[FORMAT] Task: {task_id} | Determining format for {safe_url}")
            format_str = FormatDecision.decide(intent)
            log_info(request, f"[FORMAT] Task: {task_id} | Format decided: {format_str}")
            
            await TaskStateManager.publish_progress(task_id, {
                'status': 'fetching_info',
                'progress': 15,
                'message': f'フォーマット決定: {format_str} (1/3)'
            })
        except Exception as e:
            log_error(request, f"[FORMAT ERROR] Task: {task_id} | {str(e)}")
            raise Exception(f"フォーマット決定エラー: {str(e)}")
        
        # 2. Get Filename (metadata)
        await TaskStateManager.publish_progress(task_id, {
            'status': 'fetching_info',
            'progress': 25,
            'message': 'ファイル名を取得中... (2/3)'
        })
        
        filename_cmd = YTDLPCommandBuilder.build_filename_command(intent.url, format_str)
        try:
            log_info(request, f"[METADATA] Task: {task_id} | Fetching filename for {safe_url}")
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
                    
                log_info(request, f"[METADATA] Task: {task_id} | Video Title: {raw_filename}")
                log_info(request, f"[METADATA] Task: {task_id} | Sanitized Filename: {filename}")
            else:
                raise Exception("Filename retrieval failed")
        except asyncio.TimeoutError:
            log_error(request, f"[METADATA TIMEOUT] Task: {task_id} | Filename fetch timeout for {safe_url}")
            video_id = hash_stable(intent.url)[:8]
            ext = intent.file_format if intent.file_format else ('mp3' if intent.audio_only else 'mp4')
            filename = f"video_{video_id}.{ext}"
            log_info(request, f"[METADATA] Task: {task_id} | Fallback filename: {filename}")
        except Exception as e:
            log_error(request, f"[METADATA ERROR] Task: {task_id} | {str(e)}")
            video_id = hash_stable(intent.url)[:8]
            ext = intent.file_format if intent.file_format else ('mp3' if intent.audio_only else 'mp4')
            filename = f"video_{video_id}.{ext}"
            log_info(request, f"[METADATA] Task: {task_id} | Fallback filename: {filename}")

        await TaskStateManager.publish_progress(task_id, {
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
        
        log_info(request, f"[DOWNLOAD] Task: {task_id} | Starting download with aria2c (16 connections)")
        log_info(request, f"[DOWNLOAD] Task: {task_id} | Temp path: {temp_path_template}")
        
        await TaskStateManager.update_task(task_id, {
            'status': 'downloading',
            'filename': filename,
            'video_title': filename
        })
        
        await TaskStateManager.publish_progress(task_id, {
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
        
        # Store process locally for cancellation
        local_processes[task_id] = process
        
        stderr_lines = deque(maxlen=STDERR_MAX_LINES)
        last_progress_time = time.time()
        
        async def parse_progress():
            """
            Parse yt-dlp/aria2c progress and broadcast
            """
            nonlocal last_progress_time
            try:
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    
                    progress_data = {'status': 'downloading'}
                    updated = False
                    
                    # Pattern 1: yt-dlp standard output
                    if '[download]' in decoded:
                        percent_match = re.search(r'(\d+\.\d+)%', decoded)
                        if percent_match:
                            percentage = float(percent_match.group(1))
                            scaled_percentage = 40 + (percentage * 0.55)
                            progress_data['progress'] = round(scaled_percentage, 1)
                            updated = True
                        
                        size_match = re.search(r'of\s+([\d.]+[KMG]iB)', decoded)
                        if size_match:
                            progress_data['total_size'] = size_match.group(1)
                        
                        speed_match = re.search(r'at\s+([\d.]+[KMG]iB/s)', decoded)
                        if speed_match:
                            progress_data['speed'] = speed_match.group(1)
                        
                        eta_match = re.search(r'ETA\s+(\d{2}:\d{2})', decoded)
                        if eta_match:
                            progress_data['eta'] = eta_match.group(1)
                            
                    # Pattern 2: aria2c output (via yt-dlp)
                    elif '[#' in decoded and 'CN:' in decoded:
                        percent_match = re.search(r'\((\d+)%\)', decoded)
                        if percent_match:
                            percentage = float(percent_match.group(1))
                            scaled_percentage = 40 + (percentage * 0.55)
                            progress_data['progress'] = round(scaled_percentage, 1)
                            updated = True
                            
                        size_match = re.search(r'/([\d.]+[KMG]iB)', decoded)
                        if size_match:
                            progress_data['total_size'] = size_match.group(1)
                            
                        speed_match = re.search(r'DL:([\d.]+[KMG]iB/s)', decoded)
                        if speed_match:
                            progress_data['speed'] = speed_match.group(1)
                            
                        eta_match = re.search(r'ETA:(\w+)', decoded)
                        if eta_match:
                            progress_data['eta'] = eta_match.group(1)
                            
                    # Check for processing
                    elif 'Destination:' in decoded or 'Merging' in decoded or '[Merger]' in decoded:
                        log_info(request, f"[PROCESSING] Task: {task_id} | Post-processing started")
                        await TaskStateManager.publish_progress(task_id, {
                            'status': 'processing',
                            'progress': 95,
                            'message': 'ファイルを処理中...'
                        })
                        continue

                    # Broadcast update if enough time passed
                    current_time = time.time()
                    if updated and current_time - last_progress_time >= 0.5:
                        await TaskStateManager.update_task(task_id, progress_data)
                        await TaskStateManager.publish_progress(task_id, progress_data)
                        last_progress_time = current_time
                        
            except Exception as e:
                log_error(request, f"[PROGRESS ERROR] Task: {task_id} | {str(e)}")
        
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
            
            # Remove from local processes immediately
            if task_id in local_processes:
                del local_processes[task_id]

            # Check if cancelled via flag
            task_data = await TaskStateManager.get_task(task_id)
            if task_data and task_data.get('cancelled', False):
                 log_info(request, f"[CANCELLED] Task: {task_id}")
                 raise Exception("ダウンロードがキャンセルされました")
            
            if returncode != 0:
                error_summary = '\n'.join(stderr_lines)
                log_error(request, f"[DOWNLOAD FAILED] Task: {task_id} | Return code: {returncode} | Error: {error_summary[:200]}")
                
                await TaskStateManager.update_task(task_id, {
                    'status': 'error',
                    'error': error_summary[:200]
                })
                await TaskStateManager.publish_progress(task_id, {
                    'status': 'error',
                    'message': f'エラー: {error_summary[:100]}',
                    'error': error_summary[:200]
                })
                raise Exception(f"yt-dlp failed: {error_summary[:200]}")
            
            await TaskStateManager.update_task(task_id, {'progress': 100.0, 'status': 'completed'})
            
            log_info(request, f"[DOWNLOAD COMPLETE] Task: {task_id} | Filename: {filename}")
            
            await TaskStateManager.publish_progress(task_id, {
                'status': 'completed',
                'progress': 100,
                'filename': filename,
                'message': 'ダウンロード完了！'
            })
                
        except asyncio.TimeoutError:
            log_error(request, f"[TIMEOUT] Task: {task_id} | Download timeout after {DOWNLOAD_TIMEOUT}s")
            await TaskStateManager.update_task(task_id, {'status': 'error', 'error': 'Download timeout'})
            process.kill()
            await process.wait()
            raise Exception("ダウンロードがタイムアウトしました")
        except Exception as e:
            log_error(request, f"[PROCESS ERROR] Task: {task_id} | {str(e)}")
            await TaskStateManager.update_task(task_id, {'status': 'error', 'error': str(e)})
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
            log_error(request, f"[FILE NOT FOUND] Task: {task_id} | No file found after download")
            raise HTTPException(status_code=500, detail="ダウンロード完了後にファイルが見つかりません")
        
        final_temp_path = os.path.join(TEMP_DIR, found_files[0])
        file_size = os.path.getsize(final_temp_path)
        
        await TaskStateManager.update_task(task_id, {
            'file_path': final_temp_path,
            'file_size': file_size
        })
        
        log_info(request, f"[DOWNLOAD SUCCESS] Task: {task_id} | Size: {file_size / 1024 / 1024:.1f} MB | Path: {final_temp_path}")
        
        return final_temp_path, filename, file_size

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
    
    # Initialize task tracking
    initial_state = {
        'task_id': task_id,
        'status': 'queued',
        'progress': 0.0,
        'speed': None,
        'eta': None,
        'filename': None,
        'video_title': None,
        'url': str(video_request.url),
        'file_path': None,
        'file_size': None,
        'error': None,
        'cancelled': False,
        'created_at': time.time()
    }
    await TaskStateManager.save_task(task_id, initial_state)
    
    log_info(request, f"[TASK CREATED] Task ID: {task_id} | URL: {safe_url_for_log(str(video_request.url))}")
    
    # Start background download
    async def background_download():
        try:
            intent = video_request.to_intent()
            await DownloadService.download_with_progress(intent, locale, request, task_id)
        except Exception as e:
            log_error(request, f"[BACKGROUND ERROR] Task: {task_id} | {str(e)}")
            await TaskStateManager.update_task(task_id, {
                'status': 'error',
                'error': str(e)
            })
            await TaskStateManager.publish_progress(task_id, {
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

@router.get("/task/{task_id}")
async def get_task_status(task_id: str):
    """
    Get status of a specific download task
    """
    task_info = await TaskStateManager.get_task(task_id)
    if not task_info:
        raise HTTPException(status_code=404, detail="Task not found")
    
    return JSONResponse(task_info)

@router.get("/task/lists")
async def list_tasks():
    """
    List all active download tasks
    """
    tasks = await TaskStateManager.get_active_tasks_list()
    
    return JSONResponse({
        'total': len(tasks),
        'tasks': tasks
    })

@router.post("/download/cancel/{task_id}")
async def cancel_download(task_id: str, request: Request):
    """
    Cancel an ongoing download
    """
    # Mark in state first
    await TaskStateManager.update_task(task_id, {'cancelled': True, 'status': 'cancelled'})
    
    # If the process is local to this worker, kill it
    if task_id in local_processes:
        process = local_processes[task_id]
        try:
            process.kill()
            await process.wait()
            log_info(request, f"[CANCELLED] Task: {task_id} | Local process killed")
        except:
            pass
            
    # Broadcast cancellation
    await TaskStateManager.publish_progress(task_id, {
        'status': 'cancelled',
        'message': 'ダウンロードがキャンセルされました'
    })
    
    return JSONResponse({
        'task_id': task_id,
        'status': 'cancelled',
        'message': 'Cancellation requested'
    })

@router.websocket("/download/progress/ws/{task_id}")
async def websocket_progress(websocket: WebSocket, task_id: str):
    """
    WebSocket endpoint handling both Redis Pub/Sub and In-Memory fallback
    """
    await websocket.accept()
    
    redis = get_redis()
    
    # Send initial state
    task_info = await TaskStateManager.get_task(task_id)
    if task_info:
        await websocket.send_text(json.dumps({
            'task_id': task_id,
            'status': task_info.get('status'),
            'progress': task_info.get('progress'),
            'filename': task_info.get('filename'),
            'message': '接続済み'
        }))

    if redis:
        # Redis Pub/Sub Mode
        pubsub = redis.pubsub()
        channel = f"task:{task_id}:progress"
        try:
            await pubsub.subscribe(channel)
            async def listener():
                async for message in pubsub.listen():
                    if message['type'] == 'message':
                        await websocket.send_text(message['data'])

            listener_task = asyncio.create_task(listener())
            while True:
                try:
                    data = await websocket.receive_text()
                    if data == 'ping':
                        await websocket.send_text('pong')
                except WebSocketDisconnect:
                    break
            listener_task.cancel()
        except Exception as e:
            print(f"WebSocket error: {e}")
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
    else:
        # In-Memory Mode
        if task_id not in memory_websocket_connections:
            memory_websocket_connections[task_id] = []
        memory_websocket_connections[task_id].append(websocket)
        
        try:
            while True:
                try:
                    data = await websocket.receive_text()
                    if data == 'ping':
                        await websocket.send_text('pong')
                except WebSocketDisconnect:
                    break
        except Exception as e:
            print(f"WebSocket error: {e}")
        finally:
            if task_id in memory_websocket_connections:
                if websocket in memory_websocket_connections[task_id]:
                    memory_websocket_connections[task_id].remove(websocket)
                if not memory_websocket_connections[task_id]:
                    del memory_websocket_connections[task_id]


@router.get("/download/file/{task_id}")
async def download_file(task_id: str, request: Request):
    """
    Download the completed file
    """
    task_info = await TaskStateManager.get_task(task_id)
    if not task_info:
        raise HTTPException(status_code=404, detail="タスクが見つかりません")
    
    if task_info.get('status') != 'completed':
        raise HTTPException(status_code=400, detail=f"ダウンロードが完了していません。ステータス: {task_info.get('status')}")
    
    file_path = task_info.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")
    
    filename = task_info.get('filename', 'video.mp4')
    file_size = task_info.get('file_size', 0)
    
    log_info(request, f"[FILE TRANSFER START] Task: {task_id} | Filename: {filename} | Size: {file_size / 1024 / 1024:.1f} MB")
    
    async def generate():
        try:
            async with aiofiles.open(file_path, 'rb') as f:
                while True:
                    chunk = await f.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        finally:
            log_info(request, f"[FILE TRANSFER COMPLETE] Task: {task_id}")
            # Cleanup
            with suppress(OSError):
                os.remove(file_path)
            await TaskStateManager.delete_task(task_id)
    
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
            
            # Get all tasks via state manager
            tasks = await TaskStateManager.get_active_tasks_list()
            
            for task in tasks:
                task_id = task.get('task_id')
                if current_time - task.get('created_at', 0) > 3600:
                    file_path = task.get('file_path')
                    if file_path and os.path.exists(file_path):
                        with suppress(OSError):
                            os.remove(file_path)
                    await TaskStateManager.delete_task(task_id)

    asyncio.create_task(cleanup())
