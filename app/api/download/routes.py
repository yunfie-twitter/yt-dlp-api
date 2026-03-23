"""
Download API route handlers.
"""

import functools
import json
import os
import time
import uuid
from urllib.parse import quote

import aiofiles
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.logging import log_info
from app.core.security import SecurityValidator, UrlValidationResult
from app.i18n import i18n
from app.infra.concurrency import release_download_slot
from app.infra.rate_limit import rate_limiter
from app.infra.redis import get_redis
from app.models.request import VideoRequest
from app.utils.locale import get_locale, safe_url_for_log

from .service import DownloadService
from .state import TaskStateManager, local_processes, memory_websocket_connections

router = APIRouter()


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
        "task_id": task_id,
        "status": "pending",
        "url": str(video_request.url),
        "created_at": time.time(),
        "progress": 0,
        "message": "待機中...",
        "filename": None,
        "cancelled": False,
    }

    # Convert Request model to Internal Intent
    intent = video_request.to_intent()

    await TaskStateManager.save_task(task_id, initial_state)

    log_info(request, f"[TASK CREATED] Task ID: {task_id} | URL: {safe_url_for_log(str(video_request.url))}")

    # Start background download
    async def background_download():
        try:
            await DownloadService.download_with_progress(intent, locale, request, task_id)
        except Exception as e:
            # Error logging already handled in service
            await TaskStateManager.update_task(task_id, {"status": "error", "error": str(e)})
            release_download_slot()  # If we were using slots

    background_tasks.add_task(background_download)

    return JSONResponse({"task_id": task_id, "status": "pending", "message": "ダウンロードを開始しました"})


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

    return JSONResponse({"total": len(tasks), "tasks": tasks})


@router.post("/task/{task_id}/cancel", dependencies=[Depends(rate_limiter)])
async def cancel_task(request: Request, task_id: str):
    """
    Cancel a running task
    """
    # Mark in state first
    await TaskStateManager.update_task(task_id, {"cancelled": True, "status": "cancelled"})

    # If the process is local to this worker, kill it
    if task_id in local_processes:
        process = local_processes[task_id]
        try:
            process.terminate()
            log_info(request, f"[CANCELLED] Task: {task_id} | Local process killed")
        except Exception:
            pass

    # Broadcast cancellation
    await TaskStateManager.publish_progress(
        task_id, {"status": "cancelled", "message": "ダウンロードがキャンセルされました"}
    )

    return JSONResponse({"task_id": task_id, "status": "cancelled"})


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
        await websocket.send_text(json.dumps({"status": "pending", "message": "Connecting..."}))

    if redis:
        # Redis Pub/Sub Mode
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"progress:{task_id}")
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    # data is bytes/str
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")

                    await websocket.send_text(data)

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

    if task_info.get("status") != "completed":
        raise HTTPException(
            status_code=400, detail=f"ダウンロードが完了していません。ステータス: {task_info.get('status')}"
        )

    file_path = task_info.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="ファイルが見つかりません")

    filename = task_info.get("filename", "video.mp4")
    file_size = task_info.get("file_size", 0)

    log_info(
        request,
        f"[FILE TRANSFER START] Task: {task_id} | Filename: {filename} | Size: {file_size / 1024 / 1024:.1f} MB",
    )

    async def generate():
        try:
            async with aiofiles.open(file_path, mode="rb") as f:
                while True:
                    chunk = await f.read(4 * 1024 * 1024)  # 4MB chunks
                    if not chunk:
                        break
                    yield chunk
        finally:
            pass

    encoded_filename = quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename*=utf-8''{encoded_filename}",
        "Content-Length": str(file_size),
    }

    return StreamingResponse(generate(), media_type="application/octet-stream", headers=headers)
