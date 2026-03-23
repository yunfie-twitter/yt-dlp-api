"""
WorkerPool: manages a ProcessPoolExecutor for yt-dlp operations.

Uses concurrent.futures.ProcessPoolExecutor + asyncio.run_in_executor
to offload yt-dlp calls to pre-forked worker processes.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Optional

from app.services.ytdlp_worker import (
    worker_download,
    worker_extract_info,
    worker_get_filename,
    worker_search,
)

logger = logging.getLogger(__name__)


class WorkerPool:
    """Process pool for yt-dlp operations."""

    def __init__(self, pool_size: int = 4) -> None:
        self._pool_size = pool_size
        self._executor: Optional[ProcessPoolExecutor] = None

    def start(self) -> None:
        """Create the ProcessPoolExecutor."""
        if self._executor is not None:
            return
        self._executor = ProcessPoolExecutor(max_workers=self._pool_size)
        logger.info(f"WorkerPool started with {self._pool_size} workers")

    def stop(self) -> None:
        """Shutdown the ProcessPoolExecutor."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
            logger.info("WorkerPool stopped")

    async def run_info(self, url: str, opts: dict) -> dict:
        """Extract video info in a worker process."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, worker_extract_info, url, opts)

    async def run_download(
        self,
        url: str,
        opts: dict,
        output_template: str,
        progress_queue: Optional[Any] = None,
    ) -> dict:
        """Download video in a worker process."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            worker_download,
            url,
            opts,
            output_template,
            progress_queue,
        )

    async def run_search(self, query: str, opts: dict, limit: int) -> list[dict]:
        """Search videos in a worker process."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, worker_search, query, opts, limit)

    async def run_filename(self, url: str, opts: dict) -> str:
        """Get filename in a worker process."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, worker_get_filename, url, opts)
