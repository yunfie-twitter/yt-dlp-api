"""
Task state management for download tasks.
Supports both Redis and In-Memory modes.
"""

import asyncio
import json
import os
from typing import Dict, List, Optional

from fastapi import WebSocket

from app.config.settings import config

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


def _get_redis():
    """Lazy import to avoid circular import at module load time."""
    from app.infra.redis import get_redis

    return get_redis()


class TaskStateManager:
    """
    Abstracts task state management to support both Redis and In-Memory modes.
    """

    @staticmethod
    async def save_task(task_id: str, data: dict):
        redis = _get_redis()
        if redis:
            await redis.setex(f"task:{task_id}", 86400, json.dumps(data))
            # Also add to active set
            await redis.sadd("tasks:active", task_id)
        else:
            memory_download_tasks[task_id] = data

    @staticmethod
    async def get_task(task_id: str) -> Optional[dict]:
        redis = _get_redis()
        if redis:
            data = await redis.get(f"task:{task_id}")
            return json.loads(data) if data else None
        else:
            return memory_download_tasks.get(task_id)

    @staticmethod
    async def update_task(task_id: str, updates: dict):
        redis = _get_redis()
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
        redis = _get_redis()
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
        redis = _get_redis()
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
        redis = _get_redis()
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
        return sorted(tasks, key=lambda x: x.get("created_at", 0), reverse=True)
