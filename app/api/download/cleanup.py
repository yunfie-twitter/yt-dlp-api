"""
Scheduled cleanup for stale download tasks and temp files.
"""

import asyncio
import os
import time

from .state import TaskStateManager


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
                task_id = task.get("task_id")
                created_at = task.get("created_at", 0)

                # Expire after 1 hour?
                if current_time - created_at > 3600:
                    file_path = task.get("file_path")
                    if file_path and os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass

                    await TaskStateManager.delete_task(task_id)
        except Exception:
            pass
