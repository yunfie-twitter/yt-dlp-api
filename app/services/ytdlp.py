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
        
        # NOTE: Do NOT use --print here as it mixes with binary output in stdout
        
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
            # If file_format is provided (e.g. mp3, m4a), use it as target audio format
            if file_format:
                cmd.extend(['--audio-format', file_format])
        else:
            # Video mode
            # If file_format is provided (e.g. mp4, mkv), attempt to remux
            if file_format:
                cmd.extend(['--remux-video', file_format])
        
        return cmd