from pydantic import BaseModel, HttpUrl, Field, validator
from typing import Optional
from urllib.parse import urlparse
from app.config.settings import config
from app.models.internal import DownloadIntent, AudioFormat

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
    format: Optional[str] = Field(None, description="Custom format specification")
    audio_only: Optional[bool] = Field(False, description="Download audio only")
    audio_format: Optional[AudioFormat] = Field(None, description="Audio format")
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

    def model_post_init(self, __context):
        """Set default audio format after initialization"""
        if self.audio_format is None:
            self.audio_format = AudioFormat(config.ytdlp.default_audio_format)
    
    def to_intent(self) -> DownloadIntent:
        """Convert to download intent"""
        return DownloadIntent(
            url=str(self.url),
            audio_only=self.audio_only,
            audio_format=self.audio_format,
            quality=self.quality,
            custom_format=self.format
        )