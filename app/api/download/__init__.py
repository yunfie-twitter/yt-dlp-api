"""
Download API package.

Split from the original monolithic download.py into:
- state.py    — Task state management (TaskStateManager, global state)
- service.py  — Download service with progress tracking
- routes.py   — API route handlers
- cleanup.py  — Scheduled cleanup for stale tasks
"""

from .cleanup import cleanup_stale_tasks
from .routes import router
from .state import TaskStateManager

__all__ = ["router", "TaskStateManager", "cleanup_stale_tasks"]
