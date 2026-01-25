import functools
import uuid

from fastapi import HTTPException, Request

from app.config.settings import config
from app.i18n import i18n
from app.infra.redis import get_redis
from app.utils.locale import get_locale


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

        # In-Memory storage
        self.memory_counter = 0
        self.memory_slots = set()  # Set of active request IDs

    def _check_memory(self, request_id: str) -> bool:
        # Cleanup expired or orphan slots if needed?
        # For simplicity, we trust release_download_slot to decrement.
        # But to be safe against crashes, we might want timestamps.
        # Here we do simple counter for now.

        if self.memory_counter >= config.download.max_concurrent:
            return False

        self.memory_counter += 1
        self.memory_slots.add(request_id)
        return True

    async def __call__(self, request: Request):
        redis = get_redis()
        request_id = str(uuid.uuid4())

        allowed = True

        if redis:
            counter_key = "active_downloads_count"
            slot_key = f"active_download:{request_id}"
            slot_ttl = config.download.timeout_seconds + 60
            counter_ttl = slot_ttl * 2

            try:
                result = await redis.eval(
                    self.lua_script,
                    2,
                    counter_key,
                    slot_key,
                    config.download.max_concurrent,
                    slot_ttl,
                    counter_ttl
                )
                allowed = bool(result)

                if allowed:
                    request.state.download_slot_key = slot_key
                    request.state.download_slot_acquired = True
            except Exception:
                # Fail open or closed?
                allowed = True
        else:
            # In-Memory Mode
            allowed = self._check_memory(request_id)
            if allowed:
                request.state.memory_request_id = request_id
                request.state.download_slot_acquired = True

        if not allowed:
            locale = get_locale(request.headers.get("accept-language"))
            _ = functools.partial(i18n.get, locale=locale)
            raise HTTPException(
                status_code=503,
                detail=_("error.server_busy", max=config.download.max_concurrent)
            )

        return True

    def release_memory(self, request_id: str):
        if request_id in self.memory_slots:
            self.memory_slots.remove(request_id)
            self.memory_counter = max(0, self.memory_counter - 1)


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
    elif not redis and hasattr(request.state, 'memory_request_id'):
        concurrency_limiter.release_memory(request.state.memory_request_id)


concurrency_limiter = ConcurrencyLimiter()
