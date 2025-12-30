from dataclasses import dataclass, field
from typing import Optional, Tuple
from redis.asyncio import Redis

@dataclass
class RuntimeState:
    """Centralized runtime state"""
    redis: Optional[Redis] = None
    supported_sites: Tuple[str, ...] = field(default_factory=tuple)
    supported_sites_etag: str = ""
    ytdlp_version: str = "unknown"
    deno_version: str = "unknown"
    js_runtime: Optional[str] = None

state = RuntimeState()
