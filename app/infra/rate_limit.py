from fastapi import HTTPException, Request
from typing import Optional
from app.infra.redis import get_redis
from app.config.settings import config
from app.utils.locale import get_locale
from app.i18n import i18n
import functools

class RedisRateLimiter:
    """Redis-based rate limiter with Lua script"""
    
    def __init__(self):
        self.max_requests = config.rate_limit.max_requests
        self.window_seconds = config.rate_limit.window_seconds
        
        self.lua_script = """
        local key = KEYS[1]
        local limit = tonumber(ARGV[1])
        local window = tonumber(ARGV[2])
        
        local current = redis.call('INCR', key)
        if current == 1 then
            redis.call('EXPIRE', key, window)
        end
        
        if current > limit then
            local ttl = redis.call('TTL', key)
            return {0, ttl}
        end
        
        return {1, 0}
        """
    
    async def __call__(self, request: Request):
        if not config.rate_limit.enabled:
            return True
        
        redis = get_redis()
        if not redis:
            return True
        
        client_ip = request.client.host
        endpoint = request.url.path
        key = f"rate:{client_ip}:{endpoint}"
        
        try:
            result = await redis.eval(
                self.lua_script,
                1,
                key,
                self.max_requests,
                self.window_seconds
            )
            
            allowed, ttl = result
            
            if not allowed:
                locale = get_locale(request.headers.get("accept-language"))
                _ = functools.partial(i18n.get, locale=locale)
                raise HTTPException(
                    status_code=429,
                    detail=_("error.rate_limit", seconds=ttl),
                    headers={"Retry-After": str(ttl)}
                )
            
            return True
        except HTTPException:
            raise
        except Exception:
            return True

rate_limiter = RedisRateLimiter()