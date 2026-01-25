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
