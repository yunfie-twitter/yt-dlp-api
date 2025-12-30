from typing import List, Optional, NamedTuple
from dataclasses import dataclass
import asyncio
from config.settings import config
from core.state import state
from models.internal import AudioFormat

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
    def build_stream_command(
        url: str,
        format_str: str,
        audio_only: bool,
        audio_format: AudioFormat,
        include_filesize: bool = True
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
        
        if include_filesize:
            cmd.extend(['--print', 'before_dl:%(filesize_approx)s'])
        
        if not config.ytdlp.enable_live_streams:
            cmd.extend(['--match-filter', '!is_live'])
        
        if state.js_runtime:
            cmd.extend(['--js-runtimes', state.js_runtime])
        
        if config.logging.level != "DEBUG":
            cmd.append('--no-progress')
        
        if audio_only and audio_format == AudioFormat.mp3:
            cmd.extend(['-x', '--audio-format', 'mp3'])
        
        return cmd
