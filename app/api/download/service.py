"""
Video download service with progress tracking and aria2c support.
"""

import asyncio
import functools
import os
import re
import time
import uuid
from collections import deque
from contextlib import suppress

from fastapi import HTTPException, Request

from app.config.settings import config
from app.core.logging import log_error, log_info
from app.i18n import i18n
from app.models.internal import DownloadIntent
from app.services.format import FormatDecision
from app.services.ytdlp import SubprocessExecutor, YTDLPCommandBuilder
from app.utils.filename import sanitize_filename
from app.utils.hash import hash_stable
from app.utils.locale import safe_url_for_log

from .state import DOWNLOAD_TIMEOUT, STDERR_MAX_LINES, TEMP_DIR, TaskStateManager, local_processes


class DownloadService:
    """
    Video download service with progress tracking and aria2c support
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

        # 2. Get Filename (metadata)
        await TaskStateManager.publish_progress(
            task_id, {"status": "fetching_info", "progress": 20, "message": "ファイル名を取得中... (2/3)"}
        )

        filename_cmd = YTDLPCommandBuilder.build_filename_command(intent.url, format_str, proxy_url=intent.proxy_url)
        try:
            result = await SubprocessExecutor.run(filename_cmd)
            if result.returncode != 0:
                raise Exception(result.stderr.decode())

            raw_filename = result.stdout.decode().strip()
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

        # 4. Build Download Command with aria2c
        use_aria2c = config.download.use_aria2c

        cmd = YTDLPCommandBuilder.build_stream_command(
            intent.url,
            format_str,
            audio_only=intent.audio_only,
            file_format=intent.file_format,
            use_aria2c=use_aria2c,
            proxy_url=intent.proxy_url,
        )

        try:
            cmd.index("-o")
        except ValueError:
            cmd.extend(["-o", temp_path_template])

        # Add progress flags
        cmd = [c for c in cmd if c != "--no-progress"]
        cmd.extend(["--newline", "--progress"])

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

        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL
        )

        # Store process locally for cancellation
        local_processes[task_id] = process

        stderr_lines = deque(maxlen=STDERR_MAX_LINES)

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

                progress_data = {"status": "downloading"}
                updated = False

                # Pattern 1: yt-dlp standard output
                if "[download]" in decoded:
                    # [download]  23.5% of 100.00MiB at 5.00MiB/s ETA 00:15
                    percentage_match = re.search(r"(\d+(\.\d+)?)%", decoded)
                    if percentage_match:
                        raw_percentage = float(percentage_match.group(1))
                        # Scale 0-100 to 20-95 range (0-20 is init, 95-100 is post-processing)
                        scaled_percentage = 20 + (raw_percentage * 0.75)

                        progress_data["progress"] = round(scaled_percentage, 1)
                        updated = True

                        size_match = re.search(r"of\s+([\d.]+[KMG]iB)", decoded)
                        if size_match:
                            progress_data["total_size"] = size_match.group(1)

                        speed_match = re.search(r"at\s+([\d.]+[KMG]iB/s)", decoded)
                        if speed_match:
                            progress_data["speed"] = speed_match.group(1)

                        eta_match = re.search(r"ETA\s+(\d{2}:\d{2})", decoded)
                        if eta_match:
                            progress_data["eta"] = eta_match.group(1)

                # Pattern 2: aria2c output (via yt-dlp)
                elif "[#" in decoded and "CN:" in decoded:
                    # [#65f80b 26MiB/30MiB(86%) CN:16 DL:4.5MiB/s ETA:1s]
                    percentage_match = re.search(r"\((\d+)%\)", decoded)
                    if percentage_match:
                        raw_percentage = float(percentage_match.group(1))
                        scaled_percentage = 20 + (raw_percentage * 0.75)

                        progress_data["progress"] = round(scaled_percentage, 1)
                        updated = True

                        size_match = re.search(r"/([\d.]+[KMG]iB)", decoded)
                        if size_match:
                            progress_data["total_size"] = size_match.group(1)

                        speed_match = re.search(r"DL:([\d.]+[KMG]iB/s)", decoded)
                        if speed_match:
                            progress_data["speed"] = speed_match.group(1)

                        eta_match = re.search(r"ETA:(\w+)", decoded)
                        if eta_match:
                            progress_data["eta"] = eta_match.group(1)

                # Check for processing
                elif "Destination:" in decoded or "Merging" in decoded or "[Merger]" in decoded:
                    progress_data["progress"] = 98.0
                    progress_data["message"] = "変換・結合中..."
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

        async def drain_stderr():
            try:
                while True:
                    line = await process.stderr.readline()
                    if not line:
                        break
                    decoded = line.decode().strip()
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
            if task_info and task_info.get("cancelled"):
                log_info(request, f"[CANCELLED] Task: {task_id}")
                raise Exception("ダウンロードがキャンセルされました")

            if returncode != 0:
                error_summary = "\n".join(stderr_lines)
                log_error(
                    request,
                    f"[DOWNLOAD FAILED] Task: {task_id} | Return code: {returncode} | Error: {error_summary[:200]}",
                )

                await TaskStateManager.update_task(
                    task_id, {"status": "error", "error": f"Exit code {returncode}", "progress": 0}
                )

                await TaskStateManager.publish_progress(
                    task_id, {"status": "error", "message": "ダウンロードに失敗しました"}
                )
                raise Exception(f"yt-dlp failed: {error_summary[:200]}")

            await TaskStateManager.update_task(task_id, {"progress": 100.0, "status": "completed"})

            log_info(request, f"[DOWNLOAD COMPLETE] Task: {task_id} | Filename: {filename}")

            await TaskStateManager.publish_progress(
                task_id, {"status": "completed", "progress": 100, "message": "ダウンロード完了！"}
            )

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
