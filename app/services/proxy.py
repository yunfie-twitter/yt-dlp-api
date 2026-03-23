"""
Proxy management service for yt-dlp requests.
"""

import random
from typing import Optional

from app.config.settings import config


class ProxyService:
    """
    Manages proxy selection for yt-dlp commands.
    Supports random selection from configured list and manual override.
    """

    @staticmethod
    def is_enabled() -> bool:
        """Check if proxy is enabled and has at least one URL configured."""
        return config.proxy.enabled and len(config.proxy.urls) > 0

    @staticmethod
    def get_random() -> Optional[str]:
        """Get a random proxy URL from the configured list."""
        if not ProxyService.is_enabled():
            return None
        return random.choice(config.proxy.urls)

    @staticmethod
    def get_by_index(index: int) -> Optional[str]:
        """Get a proxy URL by index. Returns None if out of range or disabled."""
        if not ProxyService.is_enabled():
            return None
        if 0 <= index < len(config.proxy.urls):
            return config.proxy.urls[index]
        return None

    @staticmethod
    def get_list() -> list[dict]:
        """Get the list of configured proxies with index."""
        if not ProxyService.is_enabled():
            return []
        return [{"index": i, "url": url} for i, url in enumerate(config.proxy.urls)]

    @staticmethod
    def get_count() -> int:
        """Get the number of configured proxies."""
        if not ProxyService.is_enabled():
            return 0
        return len(config.proxy.urls)
