from app.models.internal import DownloadIntent, MediaMetadata
from app.config.settings import config

class FormatDecision:
    """Make format decisions"""
    
    @staticmethod
    def decide(intent: DownloadIntent) -> str:
        """Decide format string based on intent"""
        if intent.custom_format:
            # Fallback to default if custom format fails
            # This handles cases where the specified format_id doesn't exist
            return f"{intent.custom_format}/{config.ytdlp.default_format}"
        
        if intent.audio_only:
            # Logic updated to support string formats directly or simple presets
            af = str(intent.audio_format).lower() if intent.audio_format else ""
            
            if 'mp3' in af:
                return 'bestaudio' # yt-dlp will convert if -x --audio-format mp3 is passed
            elif 'm4a' in af:
                return 'bestaudio[ext=m4a]/bestaudio'
            elif 'opus' in af:
                return 'bestaudio[ext=webm]/bestaudio'
            else:
                # Default audio strategy
                return 'bestaudio/best'
        
        if intent.quality:
            return (
                f"bestvideo[ext=mp4][height<={intent.quality}]+bestaudio[ext=m4a]/"
                f"best[ext=mp4][height<={intent.quality}]/best"
            )
        
        # Default behavior: Merge best video and best audio
        # If merging is not possible (e.g. progressive stream only), fallback to 'best'
        return "bestvideo+bestaudio/best"
    
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
                return MediaMetadata(format_str=format_str, ext='webm', media_type='audio/webm')
        
        return MediaMetadata(
            format_str=format_str,
            ext='mp4',
            media_type='application/octet-stream'
        )