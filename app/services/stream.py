import asyncio
from typing import AsyncIterator, Optional
from collections import deque
from contextlib import suppress
from fastapi import HTTPException
from app.models.internal import DownloadIntent
from app.services.ytdlp import YTDLPCommandBuilder, SubprocessExecutor
from app.services.format import FormatDecision
from app.utils.hash import hash_stable
from app.utils.filename import sanitize_filename
from app.i18n import i18n
import functools

CHUNK_SIZE = 4 * 1024 * 1024
STDERR_MAX_LINES = 50

class StreamService:
    """Video streaming service"""
    
    @staticmethod
    async def stream(intent: DownloadIntent, locale: str) -> tuple[AsyncIterator[bytes], dict, Optional[int]]:
        """
        Stream video download with single yt-dlp call.
        Returns (generator, headers, content_length)
        """
        _ = functools.partial(i18n.get, locale=locale)
        
        # Decide format: use custom format if provided, else rely on FormatDecision
        format_str = FormatDecision.decide(intent)
        
        # Get filename first (synchronous-like but async execution)
        filename_cmd = YTDLPCommandBuilder.build_filename_command(intent.url, format_str)
        try:
            # We use a short timeout for filename retrieval
            result = await SubprocessExecutor.run(filename_cmd, timeout=10.0)
            if result.returncode == 0:
                raw_filename = result.stdout.decode().strip()
                filename = sanitize_filename(raw_filename)
            else:
                # Fallback to video_id if filename retrieval fails
                video_id = hash_stable(intent.url)[:8]
                # Try to guess extension from format_str or default to mp4
                ext = 'mp3' if intent.audio_only and 'mp3' in str(intent.audio_format) else 'mp4'
                filename = f"video_{video_id}.{ext}"
        except Exception:
             # Fallback on error
            video_id = hash_stable(intent.url)[:8]
            ext = 'mp3' if intent.audio_only and 'mp3' in str(intent.audio_format) else 'mp4'
            filename = f"video_{video_id}.{ext}"

        
        cmd = YTDLPCommandBuilder.build_stream_command(
            intent.url,
            format_str,
            intent.audio_only,
            intent.audio_format
        )
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        # We rely on chunked transfer encoding (no Content-Length)
        content_length = None
        
        stderr_lines = deque(maxlen=STDERR_MAX_LINES)
        
        async def drain_stderr():
            """Drain stderr to prevent buffer deadlock"""
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
        
        stderr_task = asyncio.create_task(drain_stderr())
        
        async def generate():
            """Stream generator"""
            try:
                while True:
                    chunk = await process.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                raise
            except Exception as e:
                process.kill()
                await process.wait()
                raise HTTPException(status_code=500, detail=str(e))
            finally:
                try:
                    returncode = await asyncio.wait_for(process.wait(), timeout=5.0)
                    
                    if returncode != 0:
                        error_summary = '\n'.join(stderr_lines)
                        raise HTTPException(status_code=500, detail=f"Stream failed: {error_summary[:200]}")
                    
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task
        
        # Ensure filename is quoted properly for Content-Disposition
        # Escape double quotes in filename just in case sanitize missed something or for extra safety
        safe_filename = filename.replace('"', '\\"')
        
        headers = {
            'Content-Disposition': f'attachment; filename="{safe_filename}"',
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'no-cache',
            'Accept-Ranges': 'none',
        }
        
        if content_length:
            headers['Content-Length'] = str(content_length)
        
        return generate(), headers, content_length