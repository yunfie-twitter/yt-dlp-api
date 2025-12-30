import hashlib

def hash_stable(data: str) -> str:
    """Create stable hash using SHA256"""
    return hashlib.sha256(data.encode()).hexdigest()[:16]
