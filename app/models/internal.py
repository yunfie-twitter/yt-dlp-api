from pydantic import BaseModel
from typing import Optional

class DownloadIntent(BaseModel):
    """Internal download intent (separated from HTTP concerns)"""
    url: str
    audio_only: bool
    audio_format: Optional[str]
    file_format: Optional[str]
    quality: Optional[int]
    custom_format: Optional[str]

class MediaMetadata(BaseModel):
    """Media metadata"""
    format_str: str
    ext: str
    media_type: str
