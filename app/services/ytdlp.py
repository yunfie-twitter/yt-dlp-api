from typing import List, Optional, NamedTuple
from dataclasses import dataclass
import asyncio
from app.config.settings import config
from app.core.state import state

class CompletedProcess(NamedTuple):
    """Subprocess result"""
    returncode: int
    stdout: bytes
    stderr: bytes

class SubprocessExecutor:
    """Execute subprocess with consistent error handling"""
    
    @staticmethod
    async def run(
        cmd: List[str],
        timeout: float,
        capture_stderr: bool = True
    ) -> CompletedProcess:
        """
        Run subprocess with timeout and proper cleanup.
        Prevents process leaks and ensures consistent error handling.
        """
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if capture_stderr else asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )
            
            return CompletedProcess(
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr if capture_stderr else b""
            )
            
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        except Exception:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

class YTDLPCommandBuilder:
    """Build yt-dlp commands"""
    
    @staticmethod
    def build_info_command(url: str) -> List[str]:
        """Build command for fetching video info"""
        cmd = [
            'yt-dlp',
            '--dump-json',
            '--no-playlist',
            '--socket-timeout', str(config.download.socket_timeout),
            '--retries', str(config.download.retries),
        ]
        
        if not config.ytdlp.enable_live_streams:
            cmd.extend(['--match-filter', '!is_live'])
        
        if state.js_runtime:
            cmd.extend(['--js-runtimes', state.js_runtime])
        
        cmd.append(url)
        
        return cmd
    
    @staticmethod
    def build_filename_command(url: str, format_str: str) -> List[str]:
        """Build command for fetching filename"""
        cmd = [
            'yt-dlp',
            '--get-filename',
            '-o', '%(title)s.%(ext)s',
            '--no-playlist',
            '--socket-timeout', str(config.download.socket_timeout),
            '--retries', str(config.download.retries),
            '-f', format_str,
            '--no-warnings',  # Suppress warnings that might interfere
            url
        ]
        
        if state.js_runtime:
            cmd.extend(['--js-runtimes', state.js_runtime])
            
        return cmd

    @staticmethod
    def build_get_url_command(url: str) -> List[str]:
        """Build command for fetching direct stream URL (HLS preferred)"""
        cmd = [
            'yt-dlp',
            '--get-url',
            # Prefer HLS (m3u8) for proxying, fallback to best
            '-f', 'best[protocol^=m3u8]/best',
            '--no-playlist',
            '--socket-timeout', str(config.download.socket_timeout),
            '--retries', str(config.download.retries),
        ]

        if not config.ytdlp.enable_live_streams:
            cmd.extend(['--match-filter', '!is_live'])

        if state.js_runtime:
            cmd.extend(['--js-runtimes', state.js_runtime])

        cmd.append(url)
        
        return cmd

    @staticmethod
    def build_stream_command(
        url: str,
        format_str: str,
        audio_only: bool,
        file_format: Optional[str] = None
    ) -> List[str]:
        """Build command for streaming download"""
        cmd = [
            'yt-dlp',
            url,
            '-f', format_str,
            '-o', '-',
            '--no-playlist',
            '--socket-timeout', str(config.download.socket_timeout),
            '--retries', str(config.download.retries),
        ]
        
        if not config.ytdlp.enable_live_streams:
            cmd.extend(['--match-filter', '!is_live'])
        
        if state.js_runtime:
            cmd.extend(['--js-runtimes', state.js_runtime])
        
        # Ensure progress is suppressed to keep stdout clean
        cmd.append('--no-progress')
        cmd.append('--quiet') # Suppress other outputs
        
        if audio_only:
            # Extract audio
            cmd.append('-x')
            if file_format:
                cmd.extend(['--audio-format', file_format])
            else:
                # Default to mp3 if no format specified
                cmd.extend(['--audio-format', 'mp3'])
        else:
            # Video mode
            if file_format:
                cmd.extend(['--remux-video', file_format])
                # If GPU enabled and converting video, add HW accel args
                if config.ytdlp.enable_gpu:
                     # Use NVENC for H264 conversion (common for mp4/mkv)
                     # Note: This is a best-effort configuration for NVENC
                     cmd.extend(['--postprocessor-args', 'VideoConvertor:-c:v h264_nvenc -preset p4'])
            else:
                # If no file format specified, try to merge into mp4
                cmd.extend(['--merge-output-format', 'mp4'])

        return cmd