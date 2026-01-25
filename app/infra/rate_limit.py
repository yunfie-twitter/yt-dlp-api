import functools
import time
from collections import deque
from typing import Dict

from fastapi import HTTPException, Request

from app.config.settings import config
from app.i18n import i18n
from app.infra.redis import get_redis
from app.utils.locale import get_locale


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

        # In-memory storage: {key: deque([timestamps])}
        self.memory_store: Dict[str, deque] = {}

    def _check_memory(self, key: str) -> tuple[bool, int]:
        now = time.time()
        if key not in self.memory_store:
            self.memory_store[key] = deque()

        timestamps = self.memory_store[key]

        # Remove old timestamps
        while timestamps and now - timestamps[0] > self.window_seconds:
            timestamps.popleft()

        if len(timestamps) >= self.max_requests:
            # Calculate TTL based on the oldest request
            oldest = timestamps[0]
            reset_time = oldest + self.window_seconds
            ttl = int(max(0, reset_time - now))
            return False, ttl

        timestamps.append(now)
        return True, 0

    async def __call__(self, request: Request):
        if not config.rate_limit.enabled:
            return True

        redis = get_redis()
        client_ip = request.client.host
        endpoint = request.url.path
        key = f"rate:{client_ip}:{endpoint}"

        allowed = True
        ttl = 0

        if redis:
            try:
                result = await redis.eval(
                    self.lua_script,
                    1,
                    key,
                    self.max_requests,
                    self.window_seconds
                )
                allowed, ttl = result
            except Exception:
                # Fallback to allow if Redis call fails mid-operation?
                # Or fallback to memory? Better to fail open or use memory if persistent failure.
                # Here assuming if redis object exists, it works.
                # If call fails, we could fallback to memory but that splits state.
                # Simple fallback: Allow request to prevent blocking due to Redis error.
                return True
        else:
            # In-Memory Mode
            allowed, ttl = self._check_memory(key)

        if not allowed:
            locale = get_locale(request.headers.get("accept-language"))
            _ = functools.partial(i18n.get, locale=locale)
            raise HTTPException(
                status_code=429,
                detail=_("error.rate_limit", seconds=ttl),
                headers={"Retry-After": str(ttl)}
            )

        return True


rate_limiter = RedisRateLimiter()
