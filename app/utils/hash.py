import hashlib


def hash_stable(data: str) -> str:
    """Generate stable SHA256 hash for caching keys"""
    return hashlib.sha256(data.encode()).hexdigest()[:16]
