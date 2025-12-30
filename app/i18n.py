from typing import Dict, Optional
import json
import os

class I18n:
    """Simple internationalization handler"""
    
    def __init__(self, default_locale: str = "en"):
        self.default_locale = default_locale
        self.translations: Dict[str, Dict[str, str]] = {}
        self._load_translations()
    
    def _load_translations(self):
        """Load translation files"""
        locales_dir = os.path.join(os.path.dirname(__file__), "locales")
        
        if not os.path.exists(locales_dir):
            return
        
        for filename in os.listdir(locales_dir):
            if filename.endswith(".json"):
                locale = filename.replace(".json", "")
                filepath = os.path.join(locales_dir, filename)
                
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        self.translations[locale] = json.load(f)
                except Exception as e:
                    print(f"Failed to load locale {locale}: {str(e)}")
    
    def get(self, key: str, locale: Optional[str] = None, **kwargs) -> str:
        """Get translated string"""
        locale = locale or self.default_locale
        
        # Fallback to default locale if not found
        translation = self.translations.get(locale, {}).get(key)
        
        if not translation:
            translation = self.translations.get(self.default_locale, {}).get(key, key)
        
        # Format with kwargs
        try:
            return translation.format(**kwargs)
        except:
            return translation

i18n = I18n(default_locale="en")
