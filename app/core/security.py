import asyncio
import ipaddress
import socket
from enum import Enum, auto
from urllib.parse import urlparse

from app.config.settings import config
from app.infra.redis import get_redis
from app.utils.hash import hash_stable

SSRF_CACHE_TTL = 300


class UrlValidationResult(Enum):
    """URL validation result without throwing exceptions"""
    OK = auto()
    BLOCKED = auto()
    INVALID = auto()


class SecurityValidator:
    """
    Validate URL security without throwing exceptions.
    Returns result enum for separation of concerns.
    """

    @staticmethod
    async def validate_url(url: str) -> UrlValidationResult:
        """
        Validate URL against SSRF attacks.
        Uses async DNS resolution and Redis caching.
        """
        if not config.security.enable_ssrf_protection:
            return UrlValidationResult.OK

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname

            if not hostname:
                return UrlValidationResult.INVALID

            # Check cache first
            redis = get_redis()
            if redis:
                cache_key = f"ssrf:{hash_stable(hostname)}"
                cached = await redis.get(cache_key)
                if cached == "ok":
                    return UrlValidationResult.OK
                if cached == "blocked":
                    return UrlValidationResult.BLOCKED

            # Async DNS resolution
            try:
                addr_info = await asyncio.to_thread(
                    socket.getaddrinfo,
                    hostname,
                    None
                )
                ips = [info[4][0] for info in addr_info]
            except socket.gaierror:
                # DNS failed - cache as OK and let yt-dlp handle it
                if redis:
                    await redis.setex(cache_key, SSRF_CACHE_TTL, "ok")
                return UrlValidationResult.OK

            # Check all resolved IPs
            is_blocked = False
            for ip_str in ips:
                try:
                    ip = ipaddress.ip_address(ip_str)

                    if not config.security.allow_localhost and ip.is_loopback:
                        is_blocked = True
                        break

                    if not config.security.allow_private_ips and ip.is_private:
                        is_blocked = True
                        break

                    if ip.is_link_local or ip.is_multicast:
                        is_blocked = True
                        break

                except ValueError as e:
                    if "does not appear to be" not in str(e):
                        return UrlValidationResult.INVALID

            # Cache result
            if redis:
                await redis.setex(
                    cache_key,
                    SSRF_CACHE_TTL,
                    "blocked" if is_blocked else "ok"
                )

            return UrlValidationResult.BLOCKED if is_blocked else UrlValidationResult.OK

        except Exception:
            return UrlValidationResult.INVALID
