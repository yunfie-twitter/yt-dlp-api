from app.models.internal import DownloadIntent, MediaMetadata, AudioFormat
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
            if intent.audio_format == AudioFormat.mp3:
                return 'bestaudio'
            elif intent.audio_format == AudioFormat.m4a:
                return 'bestaudio[ext=m4a]/bestaudio'
            else:  # opus
                return 'bestaudio[ext=webm]/bestaudio'
        
        if intent.quality:
            return (
                f"bestvideo[ext=mp4][height<={intent.quality}]+bestaudio[ext=m4a]/"
                f"best[ext=mp4][height<={intent.quality}]/best"
            )
        
        return config.ytdlp.default_format
    
    @staticmethod
    def get_metadata(intent: DownloadIntent) -> MediaMetadata:
        """Get media metadata based on intent"""
        format_str = FormatDecision.decide(intent)
        
        if intent.custom_format:
            # When custom format is used, we can't be sure about the extension
            # But usually mp4 is a safe default for container if not specified
            # Ideally we would detect this from format_str, but for now fallback to mp4
            # If the user specified an audio-only format_id, ext might be wrong here
            # but yt-dlp will output the correct file extension anyway.
            # The filename extension in Content-Disposition is a hint.
            return MediaMetadata(
                format_str=format_str,
                ext='mp4',
                media_type='application/octet-stream'
            )
        
        if intent.audio_only:
            if intent.audio_format == AudioFormat.mp3:
                return MediaMetadata(format_str=format_str, ext='mp3', media_type='audio/mpeg')
            elif intent.audio_format == AudioFormat.m4a:
                return MediaMetadata(format_str=format_str, ext='m4a', media_type='audio/mp4')
            else:
                return MediaMetadata(format_str=format_str, ext='webm', media_type='audio/webm')
        
        return MediaMetadata(
            format_str=format_str,
            ext='mp4',
            media_type='application/octet-stream'
        )