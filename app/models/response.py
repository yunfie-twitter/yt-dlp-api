from pydantic import BaseModel
from typing import Optional, List, Dict, Any

class VideoInfo(BaseModel):
    id: Optional[str] = None
    title: str
    description: Optional[str] = None
    duration: Optional[int] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    uploader: Optional[str] = None
    channel: Optional[str] = None
    channel_id: Optional[str] = None
    thumbnail: Optional[str] = None
    webpage_url: str
    age_limit: Optional[int] = None
    is_live: bool = False
    formats: List[Dict[str, Any]]
    subtitles: Optional[Dict[str, Any]] = None

class SearchResult(BaseModel):
    id: Optional[str] = None
    title: str
    description: Optional[str] = None
    duration: Optional[int] = None
    view_count: Optional[int] = None
    uploader: Optional[str] = None
    thumbnail: Optional[str] = None
    webpage_url: str

class SearchResponse(BaseModel):
    query: str
    results: List[SearchResult]
