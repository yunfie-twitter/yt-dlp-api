"""
Video download service with progress tracking via yt-dlp Python API worker pool.
"""

import asyncio
import functools
import multiprocessing
import os
import time
import uuid
from contextlib import suppress

from fastapi import HTTPException, Request

from app.config.settings import config
from app.core.logging import log_error, log_info
from app.core.state import state
from app.i18n import i18n
from app.models.internal import DownloadIntent
from app.services.format import FormatDecision
from app.services.ytdlp import YTDLPCommandBuilder
from app.utils.filename import sanitize_filename
from app.utils.hash import hash_stable
from app.utils.locale import safe_url_for_log

from .state import DOWNLOAD_TIMEOUT, TEMP_DIR, TaskStateManager


class DownloadService:
    """
    Video download service with progress tracking via worker pool
    """

    @staticmethod
    async def download_with_progress(
        intent: DownloadIntent, locale: str, request: Request, task_id: str
    ) -> tuple[str, str, int]:
        """
        Download to temp file with WebSocket progress updates via Redis Pub/Sub or In-Memory broadcast.
        """
        _ = functools.partial(i18n.get, locale=locale)
        safe_url = safe_url_for_log(intent.url)

        # Log: Download started
        log_info(request, f"[DOWNLOAD START] Task: {task_id} | URL: {safe_url}")

        # Update status: fetching_info
        await TaskStateManager.update_task(
            task_id, {"status": "fetching_info", "progress": 0, "message": "動画情報を取得中... (1/3)"}
        )

        await TaskStateManager.publish_progress(
            task_id, {"status": "fetching_info", "progress": 0, "message": "動画情報を取得中... (0/3)"}
        )

        # 1. Determine Format String
        try:
            format_str = FormatDecision.decide(intent)
            log_info(request, f"[FORMAT] Task: {task_id} | Format decided: {format_str}")

            await TaskStateManager.publish_progress(
                task_id, {"status": "fetching_info", "progress": 10, "message": "フォーマット決定完了 (1/3)"}
            )

        except Exception as e:
            log_error(request, f"[FORMAT ERROR] Task: {task_id} | {str(e)}")
            raise Exception(f"フォーマット決定エラー: {str(e)}") from e

        # 2. Get Filename (metadata) via worker pool
        await TaskStateManager.publish_progress(
            task_id, {"status": "fetching_info", "progress": 20, "message": "ファイル名を取得中... (2/3)"}
        )

        filename_opts = YTDLPCommandBuilder.build_filename_opts(intent.url, format_str, proxy_url=intent.proxy_url)
        try:
            raw_filename = await state.worker_pool.run_filename(intent.url, filename_opts)
            filename = sanitize_filename(raw_filename)

            # Apply file format if specified
            if intent.file_format:
                if intent.file_format == "mp3":
                    root, _ = os.path.splitext(filename)
                    filename = f"{root}.mp3"

            log_info(request, f"[METADATA] Task: {task_id} | Video Title: {raw_filename}")
            log_info(request, f"[METADATA] Task: {task_id} | Sanitized Filename: {filename}")

            await TaskStateManager.update_task(task_id, {"filename": filename, "video_title": raw_filename})

        except Exception as e:
            log_error(request, f"[METADATA ERROR] Task: {task_id} | {str(e)}")
            # Fallback filename if fetch fails
            filename = f"video_{hash_stable(intent.url)}.mp4"

        # 3. Prepare Temp Directory
        temp_id = str(uuid.uuid4())
        temp_path_template = os.path.join(TEMP_DIR, f"{temp_id}.%(ext)s")

        # 4. Build Download Options
        use_aria2c = config.download.use_aria2c

        download_opts = YTDLPCommandBuilder.build_download_opts(
            intent.url,
            format_str,
            audio_only=intent.audio_only,
            file_format=intent.file_format,
            use_aria2c=use_aria2c,
            proxy_url=intent.proxy_url,
        )

        if use_aria2c:
            log_info(
                request,
                f"[DOWNLOAD] Task: {task_id} | Starting download with aria2c "
                f"({config.download.aria2c_max_connections} connections)",
            )
        else:
            log_info(request, f"[DOWNLOAD] Task: {task_id} | Starting download with standard downloader")

        log_info(request, f"[DOWNLOAD] Task: {task_id} | Temp path: {temp_path_template}")

        await TaskStateManager.update_task(
            task_id, {"status": "downloading", "progress": 0, "filename": filename, "video_title": filename}
        )

        await TaskStateManager.publish_progress(
            task_id,
            {
                "status": "downloading",
                "progress": 0,
                "message": f"ダウンロード中... ({'aria2c' if use_aria2c else 'standard'})",
            },
        )

        # 5. Run download in worker pool with progress tracking
        progress_queue: multiprocessing.Queue = multiprocessing.Queue()

        # Store a cancellation sentinel so we can mark cancelled state
        cancel_event = asyncio.Event()

        async def monitor_progress():
            """Read progress from the multiprocessing queue and publish updates."""
            last_progress_time = 0.0
            loop = asyncio.get_running_loop()

            while not cancel_event.is_set():
                try:
                    msg = await loop.run_in_executor(None, _queue_get_nowait, progress_queue)
                except Exception:
                    await asyncio.sleep(0.3)
                    continue

                if msg is None:
                    await asyncio.sleep(0.3)
                    continue

                status = msg.get("status")
                progress_data: dict = {"status": "downloading"}
                updated = False

                if status == "downloading":
                    total = msg.get("total_bytes")
                    downloaded = msg.get("downloaded_bytes")
                    if total and downloaded and total > 0:
                        raw_pct = (downloaded / total) * 100
                        scaled = 20 + (raw_pct * 0.75)
                        progress_data["progress"] = round(scaled, 1)
                        updated = True

                    speed = msg.get("speed")
                    if speed:
                        progress_data["speed"] = _format_speed(speed)

                    eta = msg.get("eta")
                    if eta is not None:
                        progress_data["eta"] = _format_eta(eta)

                    if total:
                        progress_data["total_size"] = _format_bytes(total)

                elif status == "finished":
                    progress_data["progress"] = 95.0
                    progress_data["message"] = "変換・結合中..."
                    updated = True

                if updated:
                    now = time.time()
                    if now - last_progress_time > 0.5:
                        await TaskStateManager.update_task(task_id, progress_data)
                        await TaskStateManager.publish_progress(task_id, progress_data)
                        last_progress_time = now

        progress_task = asyncio.create_task(monitor_progress())

        try:
            result = await asyncio.wait_for(
                state.worker_pool.run_download(
                    intent.url,
                    download_opts,
                    temp_path_template,
                    progress_queue,
                ),
                timeout=DOWNLOAD_TIMEOUT,
            )

            # Stop progress monitor
            cancel_event.set()
            await progress_task

            # Check if cancelled via flag
            task_info = await TaskStateManager.get_task(task_id)
            if task_info and task_info.get("cancelled"):
                log_info(request, f"[CANCELLED] Task: {task_id}")
                raise Exception("ダウンロードがキャンセルされました")

            retcode = result.get("returncode", 1)
            if retcode != 0:
                log_error(request, f"[DOWNLOAD FAILED] Task: {task_id} | Return code: {retcode}")

                await TaskStateManager.update_task(
                    task_id, {"status": "error", "error": f"Exit code {retcode}", "progress": 0}
                )

                await TaskStateManager.publish_progress(
                    task_id, {"status": "error", "message": "ダウンロードに失敗しました"}
                )
                raise Exception(f"yt-dlp failed with exit code {retcode}")

            await TaskStateManager.update_task(task_id, {"progress": 100.0, "status": "completed"})

            log_info(request, f"[DOWNLOAD COMPLETE] Task: {task_id} | Filename: {filename}")

            await TaskStateManager.publish_progress(
                task_id, {"status": "completed", "progress": 100, "message": "ダウンロード完了！"}
            )

        except asyncio.TimeoutError:
            cancel_event.set()
            with suppress(asyncio.CancelledError):
                progress_task.cancel()
                await progress_task
            log_error(request, f"[TIMEOUT] Task: {task_id} | Download timeout after {DOWNLOAD_TIMEOUT}s")
            raise Exception("ダウンロードがタイムアウトしました") from None
        except Exception as e:
            cancel_event.set()
            with suppress(asyncio.CancelledError):
                progress_task.cancel()
                await progress_task
            log_error(request, f"[PROCESS ERROR] Task: {task_id} | {str(e)}")
            raise e

        # 6. Locate Output File
        found_files = [f for f in os.listdir(TEMP_DIR) if f.startswith(temp_id)]

        if not found_files:
            log_error(request, f"[FILE NOT FOUND] Task: {task_id} | No file found after download")
            raise HTTPException(status_code=500, detail="ダウンロード完了後にファイルが見つかりません")

        final_temp_path = os.path.join(TEMP_DIR, found_files[0])
        file_size = os.path.getsize(final_temp_path)

        await TaskStateManager.update_task(task_id, {"file_path": final_temp_path, "file_size": file_size})

        log_info(
            request,
            f"[DOWNLOAD SUCCESS] Task: {task_id} | Size: {file_size / 1024 / 1024:.1f} MB | Path: {final_temp_path}",
        )

        return final_temp_path, filename, file_size


def _queue_get_nowait(q: multiprocessing.Queue):
    """Non-blocking get from multiprocessing.Queue. Returns None if empty."""
    try:
        return q.get_nowait()
    except Exception:
        return None


def _format_speed(speed: float) -> str:
    """Format speed in bytes/s to human-readable string."""
    if speed >= 1024 * 1024:
        return f"{speed / 1024 / 1024:.1f}MiB/s"
    if speed >= 1024:
        return f"{speed / 1024:.1f}KiB/s"
    return f"{speed:.0f}B/s"


def _format_bytes(size: float) -> str:
    """Format bytes to human-readable string."""
    if size >= 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024 / 1024:.2f}GiB"
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.2f}MiB"
    if size >= 1024:
        return f"{size / 1024:.1f}KiB"
    return f"{size:.0f}B"


def _format_eta(eta: int) -> str:
    """Format ETA seconds to MM:SS string."""
    minutes, seconds = divmod(int(eta), 60)
    return f"{minutes:02d}:{seconds:02d}"
