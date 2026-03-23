from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

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
    worker_pool: Any = None


state = RuntimeState()
