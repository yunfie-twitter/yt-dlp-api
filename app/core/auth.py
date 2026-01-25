import json
import secrets
import time

from fastapi import HTTPException, Request, Security
from fastapi.security.api_key import APIKeyHeader

from app.config.settings import config
from app.infra.redis import get_redis

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_api_key(
    api_key_header: str = Security(API_KEY_HEADER),
):
    """
    Validate API Key
    """
    if not config.auth.api_key_enabled:
        return True

    if not api_key_header:
        raise HTTPException(
            status_code=403,
            detail="Could not validate credentials"
        )

    redis = get_redis()
    if redis:
        # Check Redis for key
        key_data = await redis.get(f"apikey:{api_key_header}")
        if not key_data:
            raise HTTPException(
                status_code=403,
                detail="Invalid API Key"
            )
        return json.loads(key_data)
    else:
        # Fallback for no Redis (Development only - usually we want Redis)
        # If Redis is missing but Auth enabled, fail secure
        raise HTTPException(
            status_code=503,
            detail="Auth service unavailable"
        )


async def create_api_key(origin: str, description: str = None) -> dict:
    """
    Generate a new API Key and store in Redis
    """
    new_key = secrets.token_urlsafe(32)
    key_data = {
        "key": new_key,
        "origin": origin,
        "description": description,
        "created_at": time.time()
    }

    redis = get_redis()
    if redis:
        # Store key (No expiration for now, or long lived)
        await redis.set(f"apikey:{new_key}", json.dumps(key_data))
        return key_data

    raise HTTPException(status_code=503, detail="Redis unavailable for key storage")


async def verify_issuance_permission(request: Request):
    """
    Verify if the request origin is allowed to issue keys
    """
    allowed_origins = config.auth.issue_allowed_origins

    if "*" in allowed_origins:
        return True

    origin = request.headers.get("origin")
    # Also check Referer as fallback if Origin missing?
    # Standard CORS check usually relies on Origin.

    if not origin or origin not in allowed_origins:
        raise HTTPException(
            status_code=403,
            detail="Origin not allowed to issue API keys"
        )
    return True
