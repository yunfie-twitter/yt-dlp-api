import asyncio
import functools
import json
import os
import re
import time
import uuid
from collections import deque
from contextlib import suppress
from typing import Dict, List, Optional
from urllib.parse import quote

import aiofiles
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from app.config.settings import config
from app.core.logging import log_error, log_info
from app.core.security import SecurityValidator, UrlValidationResult
from app.i18n import i18n
from app.infra.concurrency import release_download_slot
from app.infra.rate_limit import rate_limiter
from app.infra.redis import get_redis
from app.models.internal import DownloadIntent
from app.models.request import VideoRequest
from app.services.format import FormatDecision
from app.services.ytdlp import SubprocessExecutor, YTDLPCommandBuilder
from app.utils.filename import sanitize_filename
from app.utils.hash import hash_stable
from app.utils.locale import get_locale, safe_url_for_log

router = APIRouter()

# --- Global / Shared State ---
# In a clustered environment (multiple Uvicorn workers), memory state is local to the worker.
# Redis is preferred for shared state.

local_processes: Dict[str, asyncio.subprocess.Process] = {}

# Only used if Redis is not available
memory_download_tasks: Dict[str, dict] = {}
memory_websocket_connections: Dict[str, List[WebSocket]] = {}

STDERR_MAX_LINES = 20
DOWNLOAD_TIMEOUT = config.download.timeout_seconds
TEMP_DIR = "/tmp/ytdlp_downloads"
os.makedirs(TEMP_DIR, exist_ok=True)


class TaskStateManager:
    """
    Abstracts task state management to support both Redis and In-Memory modes.
    """

    @staticmethod
    def _use_redis() -> bool:
        return get_redis() is not None

    @staticmethod
    async def save_task(task_id: str, data: dict):
        redis = get_redis()
        if redis:
            await redis.setex(f"task:{task_id}", 86400, json.dumps(data))
            # Also add to active set
            await redis.sadd("tasks:active", task_id)
        else:
            memory_download_tasks[task_id] = data

    @staticmethod
    async def get_task(task_id: str) -> Optional[dict]:
        redis = get_redis()
        if redis:
            data = await redis.get(f"task:{task_id}")
            return json.loads(data) if data else None
        else:
            return memory_download_tasks.get(task_id)

    @staticmethod
    async def update_task(task_id: str, updates: dict):
        redis = get_redis()
        if redis:
            # Need to get, update, set (watch for race conditions in high concurrency, but ok for simple progress)
            current_data = await redis.get(f"task:{task_id}")
            if current_data:
                task_data = json.loads(current_data)
                task_data.update(updates)
                await redis.setex(f"task:{task_id}", 86400, json.dumps(task_data))
        else:
            if task_id in memory_download_tasks:
                memory_download_tasks[task_id].update(updates)

    @staticmethod
    async def delete_task(task_id: str):
        redis = get_redis()
        if redis:
            await redis.delete(f"task:{task_id}")
            await redis.srem("tasks:active", task_id)
        else:
            memory_download_tasks.pop(task_id, None)

    @staticmethod
    async def publish_progress(task_id: str, message: dict):
        """
        Publish progress update.
        Redis: Pub/Sub to channel "progress:{task_id}"
        Memory: Iterate local websockets and send
        """
        redis = get_redis()
        if redis:
            await redis.publish(f"progress:{task_id}", json.dumps(message))
        else:
            # Local broadcast
            if task_id in memory_websocket_connections:
                disconnected = []
                for ws in memory_websocket_connections[task_id]:
                    try:
                        await ws.send_text(json.dumps(message))
                    except Exception:
                        disconnected.append(ws)

                for ws in disconnected:
                    memory_websocket_connections[task_id].remove(ws)

    @staticmethod
    async def get_active_tasks_list() -> List[dict]:
        """Return list of active task objects"""
        redis = get_redis()
        tasks = []
        if redis:
            task_ids = await redis.smembers("tasks:active")
            for tid in task_ids:
                t = await redis.get(f"task:{tid}")
                if t:
                    tasks.append(json.loads(t))
        else:
            tasks = list(memory_download_tasks.values())

        # Add task_id to dict if missing (it should be there usually)
        # and sort
        return sorted(tasks, key=lambda x: x.get('created_at', 0), reverse=True)


class DownloadService:
    """
    Video download service with progress tracking and aria2c support
    """

    @staticmethod
    async def download_with_progress(
        intent: DownloadIntent,
        locale: str,
        request: Request,
        task_id: str
    ) -> tuple[str, str, int]:
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
            'progress': 0,
            'message': '動画情報を取得中... (1/3)'
        })

        await TaskStateManager.publish_progress(task_id, {
            'status': 'fetching_info',
            'progress': 0,
            'message': '動画情報を取得中... (0/3)'
        })

        # 1. Determine Format String
        try:
            format_str = FormatDecision.decide(intent)
            log_info(request, f"[FORMAT] Task: {task_id} | Format decided: {format_str}")

            await TaskStateManager.publish_progress(task_id, {
                'status': 'fetching_info',
                'progress': 10,
                'message': 'フォーマット決定完了 (1/3)'
            })

        except Exception as e:
            log_error(request, f"[FORMAT ERROR] Task: {task_id} | {str(e)}")
            raise Exception(f"フォーマット決定エラー: {str(e)}") from e

        # 2. Get Filename (metadata)
        await TaskStateManager.publish_progress(task_id, {
            'status': 'fetching_info',
            'progress': 20,
            'message': 'ファイル名を取得中... (2/3)'
        })

        filename_cmd = YTDLPCommandBuilder.build_filename_command(intent.url, format_str)
        try:
            result = await SubprocessExecutor.run(filename_cmd)
            if result.returncode != 0:
                raise Exception(result.stderr.decode())

            raw_filename = result.stdout.decode().strip()
            filename = sanitize_filename(raw_filename)

            # Apply file format if specified
            if intent.file_format:
                if intent.file_format == 'mp3':
                    root, _ = os.path.splitext(filename)
                    filename = f"{root}.mp3"

            log_info(request, f"[METADATA] Task: {task_id} | Video Title: {raw_filename}")
            log_info(request, f"[METADATA] Task: {task_id} | Sanitized Filename: {filename}")

            await TaskStateManager.update_task(task_id, {
                'filename': filename,
                'video_title': raw_filename
            })

        except Exception as e:
            log_error(request, f"[METADATA ERROR] Task: {task_id} | {str(e)}")
            # Fallback filename if fetch fails
            filename = f"video_{hash_stable(intent.url)}.mp4"

        # 3. Prepare Temp Directory
        temp_id = str(uuid.uuid4())
        temp_path_template = os.path.join(TEMP_DIR, f"{temp_id}.%(ext)s")

        # 4. Build Download Command with aria2c
        use_aria2c = config.download.use_aria2c

        cmd = YTDLPCommandBuilder.build_stream_command(
            intent.url,
            format_str,
            audio_only=intent.audio_only,
            cookies_content=intent.cookies,
            use_aria2c=use_aria2c
        )

        try:
            cmd.index('-o')
        except ValueError:
            cmd.extend(['-o', temp_path_template])

        # Add progress flags
        cmd = [c for c in cmd if c != '--no-progress']
        cmd.extend(['--newline', '--progress'])

        if use_aria2c:
            log_info(
                request,
                f"[DOWNLOAD] Task: {task_id} | Starting download with aria2c "
                f"({config.download.aria2c_max_connections} connections)"
            )
        else:
            log_info(request, f"[DOWNLOAD] Task: {task_id} | Starting download with standard downloader")

        log_info(request, f"[DOWNLOAD] Task: {task_id} | Temp path: {temp_path_template}")

        await TaskStateManager.update_task(task_id, {
            'status': 'downloading',
            'progress': 0,
            'filename': filename,
            'video_title': filename
        })

        await TaskStateManager.publish_progress(task_id, {
            'status': 'downloading',
            'progress': 0,
            'message': f'ダウンロード中... ({"aria2c" if use_aria2c else "standard"})'
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
        # last_progress_time = time.time() # unused

        async def parse_progress():
            """
            Read stdout/stderr stream and parse progress
            """
            nonlocal process
            # Limit update frequency to reduce Redis load
            last_progress_time = 0

            while True:
                line = await process.stdout.readline()
                if not line:
                    break

                decoded = line.decode().strip()

                progress_data = {'status': 'downloading'}
                updated = False

                # Pattern 1: yt-dlp standard output
                if '[download]' in decoded:
                    # [download]  23.5% of 100.00MiB at 5.00MiB/s ETA 00:15
                    percentage_match = re.search(r'(\d+(\.\d+)?)%', decoded)
                    if percentage_match:
                        raw_percentage = float(percentage_match.group(1))
                        # Scale 0-100 to 20-95 range (0-20 is init, 95-100 is post-processing)
                        scaled_percentage = 20 + (raw_percentage * 0.75)

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
                    # [#65f80b 26MiB/30MiB(86%) CN:16 DL:4.5MiB/s ETA:1s]
                    percentage_match = re.search(r'\((\d+)%\)', decoded)
                    if percentage_match:
                        raw_percentage = float(percentage_match.group(1))
                        scaled_percentage = 20 + (raw_percentage * 0.75)

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
                    progress_data['progress'] = 98.0
                    progress_data['message'] = '変換・結合中...'
                    updated = True

                # Publish if updated and enough time passed (e.g. 0.5s)
                if updated:
                    current_time = time.time()
                    if current_time - last_progress_time > 0.5:
                        # Save to state
                        await TaskStateManager.update_task(task_id, progress_data)
                        # Publish
                        await TaskStateManager.publish_progress(task_id, progress_data)
                        last_progress_time = current_time

            # Drain remaining stderr to avoid deadlock? Usually handled by separate task.

        async def drain_stderr():
            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    # Only collect actual errors, skip warnings usually
                    # But yt-dlp prints some info to stderr too.
                    if process.returncode is None or process.returncode != 0:
                        stderr_lines.append(decoded)
            except Exception:
                pass

        progress_task = asyncio.create_task(parse_progress())
        stderr_task = asyncio.create_task(drain_stderr())

        try:
            # Wait with timeout
            returncode = await asyncio.wait_for(process.wait(), timeout=DOWNLOAD_TIMEOUT)

            # Remove from local processes immediately
            if task_id in local_processes:
                del local_processes[task_id]

            # Wait for output tasks to finish
            await progress_task
            await stderr_task

            # Check if cancelled via flag (double check)
            task_info = await TaskStateManager.get_task(task_id)
            if task_info and task_info.get('cancelled'):
                log_info(request, f"[CANCELLED] Task: {task_id}")
                raise Exception("ダウンロードがキャンセルされました")

            if returncode != 0:
                error_summary = '\n'.join(stderr_lines)
                log_error(
                    request,
                    f"[DOWNLOAD FAILED] Task: {task_id} | Return code: {returncode} | Error: {error_summary[:200]}"
                )

                await TaskStateManager.update_task(task_id, {
                    'status': 'error',
                    'error': f"Exit code {returncode}",
                    'progress': 0
                })

                await TaskStateManager.publish_progress(task_id, {
                    'status': 'error',
                    'message': 'ダウンロードに失敗しました'
                })
                raise Exception(f"yt-dlp failed: {error_summary[:200]}")

            await TaskStateManager.update_task(task_id, {'progress': 100.0, 'status': 'completed'})

            log_info(request, f"[DOWNLOAD COMPLETE] Task: {task_id} | Filename: {filename}")

            await TaskStateManager.publish_progress(task_id, {
                'status': 'completed',
                'progress': 100,
                'message': 'ダウンロード完了！'
            })

        except asyncio.TimeoutError:
            log_error(request, f"[TIMEOUT] Task: {task_id} | Download timeout after {DOWNLOAD_TIMEOUT}s")
            # Kill process
            with suppress(ProcessLookupError):
                process.terminate()
            # Give it a moment, then kill
            await asyncio.sleep(1)
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise Exception("ダウンロードがタイムアウトしました") from None
        except Exception as e:
            log_error(request, f"[PROCESS ERROR] Task: {task_id} | {str(e)}")
            # Cleanup process if running
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                    await process.wait()
            raise e

        # 5. Locate Output File
        # yt-dlp might have changed the extension or filename slightly (merging)
        # We look for files matching the temp_id in the temp directory
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

        log_info(
            request,
            f"[DOWNLOAD SUCCESS] Task: {task_id} | Size: {file_size / 1024 / 1024:.1f} MB | "
            f"Path: {final_temp_path}"
        )

        return final_temp_path, filename, file_size


@router.post("/download/start", dependencies=[Depends(rate_limiter)])
async def start_download(
    request: Request,
    video_request: VideoRequest,
    background_tasks: BackgroundTasks
):
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
        'status': 'pending',
        'url': str(video_request.url),
        'created_at': time.time(),
        'progress': 0,
        'message': '待機中...',
        'filename': None,
        'cancelled': False
    }

    # Convert Request model to Internal Intent
    intent = DownloadIntent(
        url=str(video_request.url),
        audio_only=video_request.audio_only,
        format_id=video_request.format_id,
        quality=video_request.quality,
        file_format=video_request.file_format,
        cookies=video_request.cookies
    )

    await TaskStateManager.save_task(task_id, initial_state)

    log_info(request, f"[TASK CREATED] Task ID: {task_id} | URL: {safe_url_for_log(str(video_request.url))}")

    # Start background download
    async def background_download():
        try:
            # Concurrency limit check could happen here using concurrency_limiter
            # but for now we rely on simple tasks.
            # If we want to strictly limit simultaneous *downloads* (not requests), we should acquire semaphore here.
            # Using semaphore logic from old code?
            # async with concurrency_limiter: ...
            # But BackgroundTasks runs after response.
            # For this Refactor, we just run it.
            await DownloadService.download_with_progress(intent, locale, request, task_id)
        except Exception as e:
            # Error logging already handled in service
            await TaskStateManager.update_task(task_id, {
                'status': 'error',
                'error': str(e)
            })
            release_download_slot()  # If we were using slots

    background_tasks.add_task(background_download)

    return JSONResponse({
        'task_id': task_id,
        'status': 'pending',
        'message': 'ダウンロードを開始しました'
    })


@router.get("/task/{task_id}", dependencies=[Depends(rate_limiter)])
async def get_task_status(request: Request, task_id: str):
    """
    Get current status of a task
    """
    task_info = await TaskStateManager.get_task(task_id)
    if not task_info:
        raise HTTPException(status_code=404, detail="Task not found")

    return JSONResponse(task_info)


@router.get("/tasks", dependencies=[Depends(rate_limiter)])
async def list_tasks(request: Request):
    """
    List all active tasks (Debug/Admin use mostly)
    """
    tasks = await TaskStateManager.get_active_tasks_list()

    return JSONResponse({
        'total': len(tasks),
        'tasks': tasks
    })


@router.post("/task/{task_id}/cancel", dependencies=[Depends(rate_limiter)])
async def cancel_task(request: Request, task_id: str):
    """
    Cancel a running task
    """
    # Mark in state first
    await TaskStateManager.update_task(task_id, {'cancelled': True, 'status': 'cancelled'})

    # If the process is local to this worker, kill it
    if task_id in local_processes:
        process = local_processes[task_id]
        try:
            process.terminate()
            # Give it a chance to clean up? No, just kill for speed in cancel
            # await process.wait()
            log_info(request, f"[CANCELLED] Task: {task_id} | Local process killed")
        except Exception:
            pass

    # Broadcast cancellation
    await TaskStateManager.publish_progress(task_id, {
        'status': 'cancelled',
        'message': 'ダウンロードがキャンセルされました'
    })

    return JSONResponse({
        'task_id': task_id,
        'status': 'cancelled'
    })


@router.websocket("/download/progress/ws/{task_id}")
async def websocket_progress(websocket: WebSocket, task_id: str):
    """
    WebSocket for real-time progress updates
    """
    await websocket.accept()

    redis = get_redis()

    # Send initial state
    task_info = await TaskStateManager.get_task(task_id)
    if task_info:
        await websocket.send_text(json.dumps(task_info))
    else:
        # If task doesn't exist yet, might be too early or invalid
        await websocket.send_text(json.dumps({'status': 'pending', 'message': 'Connecting...'}))

    if redis:
        # Redis Pub/Sub Mode
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"progress:{task_id}")
        try:
            async for message in pubsub.listen():
                if message['type'] == 'message':
                    data = message['data']
                    # data is bytes/str
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')

                    await websocket.send_text(data)

                    # Check for completion/error/cancellation to close socket?
                    # Or let client disconnect.
                    # Usually nice to auto-close on terminal state if desired, but keeping open is safer for UI.

        except WebSocketDisconnect:
            pass
        finally:
            await pubsub.unsubscribe(f"progress:{task_id}")
            await pubsub.close()
    else:
        # In-Memory Mode
        if task_id not in memory_websocket_connections:
            memory_websocket_connections[task_id] = []
        memory_websocket_connections[task_id].append(websocket)

        try:
            while True:
                # Keep connection open, wait for broadcast from DownloadService
                await websocket.receive_text()  # Wait for client (ping?) or just sleep
        except WebSocketDisconnect:
            if task_id in memory_websocket_connections:
                memory_websocket_connections[task_id].remove(websocket)


@router.get("/download/file/{task_id}")
async def download_file(request: Request, task_id: str):
    """
    Serve the downloaded file
    """
    task_info = await TaskStateManager.get_task(task_id)

    if not task_info:
        raise HTTPException(status_code=404, detail="タスクが見つかりません")

    if task_info.get('status') != 'completed':
        raise HTTPException(
            status_code=400,
            detail=f"ダウンロードが完了していません。ステータス: {task_info.get('status')}"
        )

    file_path = task_info.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")

    filename = task_info.get('filename', 'video.mp4')
    file_size = task_info.get('file_size', 0)

    log_info(
        request,
        f"[FILE TRANSFER START] Task: {task_id} | Filename: {filename} | "
        f"Size: {file_size / 1024 / 1024:.1f} MB"
    )

    async def generate():
        try:
            async with aiofiles.open(file_path, mode='rb') as f:
                while True:
                    chunk = await f.read(4 * 1024 * 1024)  # 4MB chunks
                    if not chunk:
                        break
                    yield chunk
        finally:
            # Cleanup after download (optional, or rely on scheduled cleaner)
            # For now, we KEEP the file until cleaner runs or explicit delete
            pass
            # If we wanted to delete immediately:
            # if os.path.exists(file_path):
            #     os.remove(file_path)
            # await TaskStateManager.delete_task(task_id)

    encoded_filename = quote(filename)
    headers = {
        'Content-Disposition': f"attachment; filename*=utf-8''{encoded_filename}",
        'Content-Length': str(file_size)
    }

    return StreamingResponse(
        generate(),
        media_type='application/octet-stream',
        headers=headers
    )


# --- Cleanup Task (Scheduled) ---
async def cleanup_stale_tasks():
    """
    Periodically clean up old tasks and files (Run via separate scheduler or on startup/shutdown logic)
    """
    while True:
        try:
            await asyncio.sleep(600)  # Every 10 minutes
            current_time = time.time()

            # Get all tasks via state manager
            tasks = await TaskStateManager.get_active_tasks_list()

            for task in tasks:
                task_id = task.get('task_id')
                created_at = task.get('created_at', 0)

                # Expire after 1 hour?
                if current_time - created_at > 3600:
                    file_path = task.get('file_path')
                    if file_path and os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass

                    await TaskStateManager.delete_task(task_id)
        except Exception:
            pass
