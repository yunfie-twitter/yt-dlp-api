from typing import Optional
import redis.asyncio as aioredis
from config.settings import config
from core.state import state

async def init_redis() -> None:
    """Initialize Redis connection with recovery"""
    try:
        redis_client = await aioredis.from_url(
            config.redis.url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=config.redis.socket_timeout
        )
        await redis_client.ping()
        
        # Recover active downloads counter
        keys = []
        cursor = 0
        while True:
            cursor, partial_keys = await redis_client.scan(
                cursor,
                match="active_download:*",
                count=100
            )
            keys.extend(partial_keys)
            if cursor == 0:
                break
        
        await redis_client.set("active_downloads_count", len(keys))
        
        state.redis = redis_client
        
        print(f"✓ Redis connected (recovered {len(keys)} active downloads)")
        
    except Exception as e:
        print(f"⚠ Redis connection failed: {str(e)}")
        state.redis = None

def get_redis() -> Optional[aioredis.Redis]:
    """Get Redis client from state"""
    return state.redis

async def close_redis() -> None:
    """Close Redis connection"""
    if state.redis:
        await state.redis.close()
        state.redis = None
