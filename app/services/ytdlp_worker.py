"""
Top-level worker functions for yt-dlp Python API.

These functions run inside ProcessPoolExecutor workers.
They MUST be top-level (not methods) to be picklable.
"""

from __future__ import annotations

import os
from typing import Any, Optional


def worker_extract_info(url: str, opts: dict) -> dict:
    """
    Extract video info using yt-dlp Python API.
    Returns the info dict directly (no JSON encoding needed).
    """
    from yt_dlp import YoutubeDL

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        # ydl.sanitize_info makes the dict JSON-serializable
        return ydl.sanitize_info(info)


def worker_download(
    url: str,
    opts: dict,
    output_template: str,
    progress_queue: Optional[Any] = None,
) -> dict:
    """
    Download video using yt-dlp Python API.

    Args:
        url: Video URL to download
        opts: yt-dlp options dict
        output_template: Output file path template (e.g. /tmp/ytdlp_downloads/{uuid}.%(ext)s)
        progress_queue: multiprocessing.Queue for progress updates (optional)

    Returns:
        dict with 'status' and 'filename' keys
    """
    from yt_dlp import YoutubeDL

    opts = dict(opts)
    opts["outtmpl"] = output_template

    last_filename: list[str] = []

    if progress_queue is not None:

        def _progress_hook(d: dict) -> None:
            status = d.get("status")
            if status == "downloading":
                progress_queue.put(
                    {
                        "status": "downloading",
                        "downloaded_bytes": d.get("downloaded_bytes"),
                        "total_bytes": d.get("total_bytes") or d.get("total_bytes_estimate"),
                        "speed": d.get("speed"),
                        "eta": d.get("eta"),
                        "filename": d.get("filename"),
                    }
                )
            elif status == "finished":
                fname = d.get("filename", "")
                last_filename.clear()
                last_filename.append(fname)
                progress_queue.put({"status": "finished", "filename": fname})

        hooks = opts.get("progress_hooks", [])
        hooks.append(_progress_hook)
        opts["progress_hooks"] = hooks

    else:
        # No progress queue: run quietly
        opts.setdefault("quiet", True)
        opts.setdefault("no_warnings", True)
        opts.setdefault("noprogress", True)

    with YoutubeDL(opts) as ydl:
        retcode = ydl.download([url])

    return {
        "returncode": retcode,
        "filename": last_filename[0] if last_filename else None,
    }


def worker_search(query: str, opts: dict, limit: int) -> list[dict]:
    """
    Search videos using yt-dlp Python API.

    Returns:
        List of info dicts for each search result.
    """
    from yt_dlp import YoutubeDL

    search_url = f"ytsearch{limit}:{query}"

    with YoutubeDL(opts) as ydl:
        result = ydl.extract_info(search_url, download=False)

    entries = result.get("entries", []) if result else []
    # Sanitize each entry for serialization
    sanitized = []
    for entry in entries:
        if entry is not None:
            sanitized.append(ydl.sanitize_info(entry))
    return sanitized


def worker_get_filename(url: str, opts: dict) -> str:
    """
    Get the output filename without downloading.

    Uses extract_info(download=False) and prepare_filename to determine
    the filename yt-dlp would produce.
    """
    from yt_dlp import YoutubeDL

    opts = dict(opts)
    opts.setdefault("outtmpl", "%(title)s.%(ext)s")

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        filename = ydl.prepare_filename(info)

    return os.path.basename(filename)
