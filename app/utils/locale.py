from typing import Optional
from urllib.parse import urlparse

from app.config.settings import config


def get_locale(accept_language: Optional[str] = None) -> str:
    """Parse Accept-Language header and return supported locale"""
    if not accept_language:
        return config.i18n.default_locale

    languages = []
    for lang in accept_language.split(","):
        parts = lang.strip().split(";")
        languages.append(parts[0].split("-")[0])

    for lang in languages:
        if lang in config.i18n.supported_locales:
            return lang

    return config.i18n.default_locale


def safe_url_for_log(url: str, max_length: int = 100) -> str:
    """Sanitize URL for logging (hide query params)"""
    try:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if len(base) > max_length:
            return base[:max_length] + "..."
        return base
    except Exception:
        return url[:max_length]
