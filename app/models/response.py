from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class VideoInfo(BaseModel):
    """Video information response"""
    title: str
    uploader: Optional[str] = None
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    formats: List[Dict[str, Any]] = []
    description: Optional[str] = None


class SearchResult(BaseModel):
    """Single search result"""
    title: str
    url: str
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    uploader: Optional[str] = None


class SearchResponse(BaseModel):
    """Search results response"""
    results: List[SearchResult]
    query: str
