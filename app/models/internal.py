from typing import Optional

from pydantic import BaseModel


class DownloadIntent(BaseModel):
    """Internal representation of download parameters after validation"""

    url: str
    audio_only: bool = False
    format_id: Optional[str] = None
    quality: Optional[int] = None
    file_format: Optional[str] = None
    cookies: Optional[str] = None
    audio_format: Optional[str] = None
    custom_format: Optional[str] = None


class MediaMetadata(BaseModel):
    """Metadata for resolved media format"""

    format_str: str
    ext: str
    media_type: str
