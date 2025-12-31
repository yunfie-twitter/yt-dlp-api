import asyncio
from typing import AsyncIterator, Optional
from collections import deque
from contextlib import suppress
from fastapi import HTTPException
from app.models.internal import DownloadIntent
from app.services.ytdlp import YTDLPCommandBuilder
from app.services.format import FormatDecision
from app.utils.hash import hash_stable
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
        
        metadata = FormatDecision.get_metadata(intent)
        
        # Generate filename
        video_id = hash_stable(intent.url)[:8]
        filename = f"video_{video_id}.{metadata.ext}"
        
        cmd = YTDLPCommandBuilder.build_stream_command(
            intent.url,
            metadata.format_str,
            intent.audio_only,
            intent.audio_format
            # Removed include_filesize=True to prevent stdout corruption
        )
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        # Previously we tried to read filesize from the first line of stdout
        # But this is unreliable and corrupts binary stream if yt-dlp doesn't output it exactly as expected
        # or if it outputs warning messages first.
        # We now rely on chunked transfer encoding (no Content-Length) which is safer for streaming.
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
                        # Only raise if we haven't yielded any data yet?
                        # If we already yielded, we can't change status code.
                        # But for now let's raise to be safe, client will get incomplete stream error.
                        raise HTTPException(status_code=500, detail=f"Stream failed: {error_summary[:200]}")
                    
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                
                stderr_task.cancel()
                with suppress(asyncio.CancelledError):
                    await stderr_task
        
        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'no-cache',
            'Accept-Ranges': 'none',
        }
        
        if content_length:
            headers['Content-Length'] = str(content_length)
        
        return generate(), headers, content_length