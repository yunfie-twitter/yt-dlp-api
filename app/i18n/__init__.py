import json
import os
from typing import Dict, Any, Optional
from app.config.settings import config

class I18n:
    """Simple internationalization helper"""
    
    def __init__(self):
        self.locales: Dict[str, Dict[str, Any]] = {}
        self.default_locale = config.i18n.default_locale
        self.load_locales()
    
    def load_locales(self):
        """Load locale files from app/locales directory"""
        locales_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "locales")
        
        if not os.path.exists(locales_dir):
            print(f"Warning: Locales directory not found at {locales_dir}")
            return

        for filename in os.listdir(locales_dir):
            if filename.endswith(".json"):
                locale_code = filename[:-5]
                try:
                    with open(os.path.join(locales_dir, filename), "r", encoding="utf-8") as f:
                        self.locales[locale_code] = json.load(f)
                except Exception as e:
                    print(f"Error loading locale {locale_code}: {e}")

    def get(self, key: str, locale: Optional[str] = None, **kwargs) -> str:
        """Get translated string by key with optional interpolation"""
        if not locale or locale not in self.locales:
            locale = self.default_locale
            
        # Fallback to English if default not found (though default should exist)
        if locale not in self.locales and "en" in self.locales:
            locale = "en"
            
        if locale not in self.locales:
            return key
            
        translations = self.locales[locale]
        
        # Handle nested keys (e.g. "error.server_busy")
        parts = key.split(".")
        value = translations
        
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                # Key not found in this locale, try default locale if different
                if locale != self.default_locale:
                    return self.get(key, self.default_locale, **kwargs)
                return key
                
        if isinstance(value, str):
            try:
                return value.format(**kwargs)
            except KeyError:
                return value
                
        return str(value)

i18n = I18n()