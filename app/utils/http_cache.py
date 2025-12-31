import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

CACHE_ROOT = os.environ.get("YTDLP_PROXY_CACHE_DIR", "cache")

CACHE_POLICY = {
    ".ts": 86400,   # 1 day
    ".m4s": 86400,
    ".m3u8": 5,     # live playlist safety
}

MAX_CACHE_BYTES = 10 * 1024 * 1024


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,  # keep query (signed CDN URLs)
            "",  # drop fragment
        )
    )


def cache_ttl_for_url(url: str) -> int:
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    return CACHE_POLICY.get(ext, 3600)


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def cache_dir_for_key(key: str) -> str:
    return os.path.join(CACHE_ROOT, key)


def cache_paths(key: str) -> tuple[str, str]:
    d = cache_dir_for_key(key)
    return os.path.join(d, "body.bin"), os.path.join(d, "meta.json")


@dataclass
class CacheMeta:
    url: str
    normalized_url: str
    status_code: int
    content_type: Optional[str]
    content_length: Optional[int]
    fetched_at: float
    ttl: int

    @property
    def expires_at(self) -> float:
        return self.fetched_at + self.ttl

    def is_fresh(self) -> bool:
        return time.time() < self.expires_at


def load_cache(normalized_url: str, ttl: int) -> Optional[tuple[bytes, CacheMeta]]:
    key = cache_key(normalized_url)
    body_path, meta_path = cache_paths(key)

    if not os.path.exists(body_path) or not os.path.exists(meta_path):
        return None

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        meta = CacheMeta(**raw)

        # TTL policy can change; respect smaller one
        meta.ttl = min(int(meta.ttl), int(ttl))

        if not meta.is_fresh():
            return None

        with open(body_path, "rb") as f:
            body = f.read()

        return body, meta
    except Exception:
        return None


def save_cache(
    normalized_url: str,
    body: bytes,
    status_code: int,
    content_type: Optional[str],
    ttl: int,
) -> None:
    key = cache_key(normalized_url)
    body_path, meta_path = cache_paths(key)
    os.makedirs(os.path.dirname(body_path), exist_ok=True)

    meta = CacheMeta(
        url=normalized_url,
        normalized_url=normalized_url,
        status_code=int(status_code),
        content_type=content_type,
        content_length=len(body) if body is not None else None,
        fetched_at=time.time(),
        ttl=int(ttl),
    )

    tmp_body = body_path + ".tmp"
    tmp_meta = meta_path + ".tmp"

    with open(tmp_body, "wb") as f:
        f.write(body)
    with open(tmp_meta, "w", encoding="utf-8") as f:
        json.dump(meta.__dict__, f, ensure_ascii=False)

    os.replace(tmp_body, body_path)
    os.replace(tmp_meta, meta_path)


def parse_range_header(range_header: str, total_size: int) -> Optional[tuple[int, int]]:
    # Supports a single range only: bytes=start-end
    if not range_header:
        return None
    if not range_header.startswith("bytes="):
        return None

    spec = range_header[len("bytes=") :].strip()
    if "," in spec:
        return None

    start_s, end_s = spec.split("-", 1)

    if start_s == "":
        # suffix range: last N bytes
        try:
            n = int(end_s)
        except ValueError:
            return None
        if n <= 0:
            return None
        start = max(0, total_size - n)
        end = total_size - 1
        return start, end

    try:
        start = int(start_s)
    except ValueError:
        return None

    if end_s == "":
        end = total_size - 1
    else:
        try:
            end = int(end_s)
        except ValueError:
            return None

    if start < 0 or end < start:
        return None

    end = min(end, total_size - 1)
    return start, end
