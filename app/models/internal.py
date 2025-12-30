from pydantic import BaseModel
from typing import Optional
from enum import Enum

class AudioFormat(str, Enum):
    """Audio format enumeration"""
    mp3 = "mp3"
    m4a = "m4a"
    opus = "opus"

class DownloadIntent(BaseModel):
    """Internal download intent (separated from HTTP concerns)"""
    url: str
    audio_only: bool
    audio_format: AudioFormat
    quality: Optional[int]
    custom_format: Optional[str]

class MediaMetadata(BaseModel):
    """Media metadata"""
    format_str: str
    ext: str
    media_type: str
