from typing import Optional
from urllib.parse import urlparse
from app.config.settings import config

def get_locale(accept_language: Optional[str] = None) -> str:
    """Extract locale from Accept-Language header"""
    if not accept_language:
        return config.i18n.default_locale
    
    languages = []
    for lang in accept_language.split(","):
        parts = lang.strip().split(";")
        locale = parts[0].split("-")[0]
        languages.append(locale)
    
    for locale in languages:
        if locale in config.i18n.supported_locales:
            return locale
    
    return config.i18n.default_locale

def safe_url_for_log(url: str) -> str:
    """Safe URL for logging"""
    try:
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        
        if config.logging.level == "DEBUG" and parsed.query:
            return f"{base_url}?..."
        
        return base_url
    except:
        return "invalid_url"