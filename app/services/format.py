from app.models.internal import DownloadIntent, MediaMetadata
from app.config.settings import config

class FormatDecision:
    """Make format decisions"""
    
    @staticmethod
    def decide(intent: DownloadIntent) -> str:
        """Decide format string based on intent"""
        if intent.custom_format:
            # If custom_format is specified with audio_format, combine them
            if intent.audio_format and not intent.audio_only:
                return f"{intent.custom_format}+{intent.audio_format}/best"
            # Fallback to default if custom format fails
            return f"{intent.custom_format}/best"
        
        if intent.audio_only:
            # For audio-only downloads, always use bestaudio
            # The actual format conversion is handled by -x --audio-format
            return 'bestaudio/best'
        
        if intent.quality:
            return (
                f"bestvideo[height<={intent.quality}]+bestaudio/"
                f"bestvideo[height<={intent.quality}]/"
                f"best[height<={intent.quality}]/"
                f"best"
            )
        
        # Default behavior: Best quality with proper fallback chain
        # Try merging best video and audio first, then fallback progressively
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    
    @staticmethod
    def get_metadata(intent: DownloadIntent) -> MediaMetadata:
        """Get media metadata based on intent"""
        format_str = FormatDecision.decide(intent)
        
        if intent.custom_format:
            return MediaMetadata(
                format_str=format_str,
                ext='mp4', # Default fallback, actual file extension determined by yt-dlp
                media_type='application/octet-stream'
            )
        
        if intent.audio_only:
            af = str(intent.audio_format).lower() if intent.audio_format else ""
            if 'mp3' in af:
                return MediaMetadata(format_str=format_str, ext='mp3', media_type='audio/mpeg')
            elif 'm4a' in af:
                return MediaMetadata(format_str=format_str, ext='m4a', media_type='audio/mp4')
            else:
                return MediaMetadata(format_str=format_str, ext='mp3', media_type='audio/mpeg')
        
        return MediaMetadata(
            format_str=format_str,
            ext='mp4',
            media_type='application/octet-stream'
        )