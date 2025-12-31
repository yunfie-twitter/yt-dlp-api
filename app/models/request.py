from pydantic import BaseModel, HttpUrl, Field, validator
from typing import Optional
from urllib.parse import urlparse
from app.config.settings import config
from app.models.internal import DownloadIntent, AudioFormat

class VideoRequest(BaseModel):
    url: HttpUrl = Field(..., description="Video URL")
    format: Optional[str] = Field(None, description="Custom format specification")
    audio_only: Optional[bool] = Field(False, description="Download audio only")
    audio_format: Optional[AudioFormat] = Field(None, description="Audio format")
    quality: Optional[int] = Field(None, description="Video quality", ge=144, le=2160)
    
    @validator('url')
    def validate_url_syntax(cls, v):
        """Validate URL syntax only (SSRF check done at endpoint)"""
        parsed = urlparse(str(v))
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("Invalid URL format")
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
