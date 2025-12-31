from fastapi import HTTPException, Request
import uuid
from app.infra.redis import get_redis
from app.config.settings import config
from app.utils.locale import get_locale
from app.i18n import i18n
import functools

class ConcurrencyLimiter:
    """Concurrent download limiter with atomic operations"""
    
    def __init__(self):
        self.lua_script = """
        local counter_key = KEYS[1]
        local slot_key = KEYS[2]
        local limit = tonumber(ARGV[1])
        local slot_ttl = tonumber(ARGV[2])
        local counter_ttl = tonumber(ARGV[3])
        
        local current = tonumber(redis.call('GET', counter_key) or "0")
        if current >= limit then
            return 0
        end
        
        redis.call('INCR', counter_key)
        redis.call('EXPIRE', counter_key, counter_ttl)
        redis.call('SETEX', slot_key, slot_ttl, "1")
        
        return 1
        """
    
    async def __call__(self, request: Request):
        redis = get_redis()
        if not redis:
            return True
        
        request_id = str(uuid.uuid4())
        counter_key = "active_downloads_count"
        slot_key = f"active_download:{request_id}"
        slot_ttl = config.download.timeout_seconds + 60
        counter_ttl = slot_ttl * 2
        
        try:
            allowed = await redis.eval(
                self.lua_script,
                2,
                counter_key,
                slot_key,
                config.download.max_concurrent,
                slot_ttl,
                counter_ttl
            )
            
            if not allowed:
                locale = get_locale(request.headers.get("accept-language"))
                _ = functools.partial(i18n.get, locale=locale)
                raise HTTPException(
                    status_code=503,
                    detail=_("error.server_busy", max=config.download.max_concurrent)
                )
            
            request.state.download_slot_key = slot_key
            request.state.download_slot_acquired = True
            
            return True
        except HTTPException:
            raise
        except Exception:
            return True

async def release_download_slot(request: Request):
    """Release download slot"""
    if not hasattr(request.state, 'download_slot_acquired') or not request.state.download_slot_acquired:
        return
    
    redis = get_redis()
    if redis and hasattr(request.state, 'download_slot_key'):
        try:
            await redis.delete(request.state.download_slot_key)
            await redis.decr("active_downloads_count")
        except Exception:
            pass

concurrency_limiter = ConcurrencyLimiter()