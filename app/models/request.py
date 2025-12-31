from pydantic import BaseModel, HttpUrl, Field, validator
from typing import Optional
from urllib.parse import urlparse
from app.config.settings import config
from app.models.internal import DownloadIntent

class InfoRequest(BaseModel):
    url: HttpUrl = Field(..., description="Video URL")

    @validator('url')
    def validate_url_syntax(cls, v):
        """Validate URL syntax only (SSRF check done at endpoint)"""
        parsed = urlparse(str(v))
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("Invalid URL format")
        return v

class VideoRequest(InfoRequest):
    format: Optional[str] = Field(None, description="Custom video format specification (yt-dlp syntax)")
    audio_only: Optional[bool] = Field(False, description="Download audio only (deprecated/implied by audio_format)")
    audio_format: Optional[str] = Field(None, description="Audio format specification (yt-dlp syntax)")
    file_format: Optional[str] = Field(None, description="Output file extension/container (e.g. mp4, mp3)")
    # Remove ge/le constraints to allow 0, validation handled in validator
    quality: Optional[int] = Field(None, description="Video quality (0 for best/auto)")
    
    @validator('quality')
    def validate_quality(cls, v):
        """Validate quality range, treating 0 as None (auto)"""
        if v is None or v == 0:
            return None
        if v < 144 or v > 2160:
            raise ValueError("Quality must be between 144 and 2160, or 0 for auto")
        return v

    def to_intent(self) -> DownloadIntent:
        """Convert to download intent"""
        # Logic: format takes precedence over audio_format.
        # If format is present, it's a video download.
        # If format is absent and audio_format is present, it's an audio download.
        
        is_audio_only = False
        if not self.format and self.audio_format:
            is_audio_only = True
        
        # Respect explicit audio_only flag if format is not set? 
        # The user says "if both written, process video".
        # If self.audio_only is True but self.format is set, we treat as video per instruction.
        # If self.audio_only is True and self.format is NOT set, we treat as audio.
        if self.audio_only and not self.format:
            is_audio_only = True

        return DownloadIntent(
            url=str(self.url),
            audio_only=is_audio_only,
            audio_format=self.audio_format,
            file_format=self.file_format,
            quality=self.quality,
            custom_format=self.format
        )