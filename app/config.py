import json
import os
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field, validator
import logging

logger = logging.getLogger(__name__)

class RedisConfig(BaseModel):
    url: str = Field(default="redis://redis:6379", description="Redis connection URL")
    socket_timeout: int = Field(default=5, description="Redis socket timeout in seconds")

class RateLimitConfig(BaseModel):
    enabled: bool = Field(default=True, description="Enable rate limiting")
    max_requests: int = Field(default=5, ge=1, description="Max requests per window")
    window_seconds: int = Field(default=60, ge=1, description="Rate limit window in seconds")

class DownloadConfig(BaseModel):
    max_concurrent: int = Field(default=10, ge=1, le=100, description="Max concurrent downloads")
    timeout_seconds: int = Field(default=3600, ge=60, description="Download timeout in seconds")
    socket_timeout: int = Field(default=10, ge=1, description="Socket timeout for yt-dlp")
    retries: int = Field(default=3, ge=0, description="Number of retries for failed downloads")

class SecurityConfig(BaseModel):
    enable_ssrf_protection: bool = Field(default=True, description="Enable SSRF protection")
    allow_private_ips: bool = Field(default=False, description="Allow private IP ranges")
    allow_localhost: bool = Field(default=False, description="Allow localhost access")

class YtDlpConfig(BaseModel):
    js_runtime: Optional[str] = Field(default=None, description="JS runtime path (e.g., deno:/usr/local/bin/deno)")
    auto_detect_runtime: bool = Field(default=True, description="Auto-detect JS runtime if not specified")
    default_format: str = Field(default="best", description="Default video format")
    default_audio_format: str = Field(default="opus", description="Default audio format")
    enable_live_streams: bool = Field(default=False, description="Allow live stream downloads")

class LoggingConfig(BaseModel):
    level: str = Field(default="INFO", description="Log level (DEBUG, INFO, WARNING, ERROR)")
    format: str = Field(default="%(message)s", description="Log format")
    enable_rich: bool = Field(default=True, description="Enable rich console logging")
    
    @validator('level')
    def validate_log_level(cls, v):
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if v.upper() not in valid_levels:
            raise ValueError(f"Log level must be one of {valid_levels}")
        return v.upper()

class I18nConfig(BaseModel):
    default_locale: str = Field(default="en", description="Default locale")
    supported_locales: list = Field(default=["en", "ja"], description="Supported locales")
    locale_dir: str = Field(default="locales", description="Locale files directory")

class ApiConfig(BaseModel):
    title: str = Field(default="yt-dlp Streaming API", description="API title")
    description: str = Field(default="Production-ready yt-dlp streaming API", description="API description")
    version: str = Field(default="4.0.0", description="API version")
    cors_origins: list = Field(default=["*"], description="CORS allowed origins")
    docs_url: str = Field(default="/docs", description="OpenAPI docs URL")
    redoc_url: str = Field(default="/redoc", description="ReDoc URL")

class Config(BaseModel):
    """Main configuration model"""
    redis: RedisConfig = Field(default_factory=RedisConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    ytdlp: YtDlpConfig = Field(default_factory=YtDlpConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    i18n: I18nConfig = Field(default_factory=I18nConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    
    @classmethod
    def load_from_file(cls, config_path: str = "config.json") -> "Config":
        """Load configuration from JSON file"""
        
        # Try to load from file
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                logger.info(f"Configuration loaded from {config_path}")
                return cls(**config_data)
            except Exception as e:
                logger.error(f"Failed to load config from {config_path}: {str(e)}")
                logger.info("Using default configuration")
        else:
            logger.warning(f"Config file {config_path} not found, using defaults")
        
        # Return default configuration
        return cls()
    
    @classmethod
    def load_from_env(cls) -> "Config":
        """Load configuration from environment variables (fallback)"""
        config_data = {}
        
        # Redis
        if os.getenv("REDIS_URL"):
            config_data["redis"] = {"url": os.getenv("REDIS_URL")}
        
        # Rate limiting
        rate_limit = {}
        if os.getenv("RATE_LIMIT_REQUESTS"):
            rate_limit["max_requests"] = int(os.getenv("RATE_LIMIT_REQUESTS"))
        if os.getenv("RATE_LIMIT_WINDOW"):
            rate_limit["window_seconds"] = int(os.getenv("RATE_LIMIT_WINDOW"))
        if rate_limit:
            config_data["rate_limit"] = rate_limit
        
        # Download
        download = {}
        if os.getenv("MAX_CONCURRENT_DOWNLOADS"):
            download["max_concurrent"] = int(os.getenv("MAX_CONCURRENT_DOWNLOADS"))
        if os.getenv("DOWNLOAD_TIMEOUT"):
            download["timeout_seconds"] = int(os.getenv("DOWNLOAD_TIMEOUT"))
        if download:
            config_data["download"] = download
        
        # Security
        security = {}
        if os.getenv("ENABLE_SSRF_PROTECTION"):
            security["enable_ssrf_protection"] = os.getenv("ENABLE_SSRF_PROTECTION").lower() == "true"
        if security:
            config_data["security"] = security
        
        # yt-dlp
        ytdlp = {}
        if os.getenv("YT_DLP_JS_RUNTIME"):
            ytdlp["js_runtime"] = os.getenv("YT_DLP_JS_RUNTIME")
        if ytdlp:
            config_data["ytdlp"] = ytdlp
        
        # Logging
        logging_config = {}
        if os.getenv("LOG_LEVEL"):
            logging_config["level"] = os.getenv("LOG_LEVEL")
        if logging_config:
            config_data["logging"] = logging_config
        
        # i18n
        i18n = {}
        if os.getenv("DEFAULT_LOCALE"):
            i18n["default_locale"] = os.getenv("DEFAULT_LOCALE")
        if i18n:
            config_data["i18n"] = i18n
        
        return cls(**config_data) if config_data else cls()
    
    def save_to_file(self, config_path: str = "config.json"):
        """Save configuration to JSON file"""
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(self.dict(), f, indent=2, ensure_ascii=False)
            logger.info(f"Configuration saved to {config_path}")
        except Exception as e:
            logger.error(f"Failed to save config to {config_path}: {str(e)}")
    
    def dict(self, **kwargs) -> Dict[str, Any]:
        """Convert to dictionary"""
        return super().dict(exclude_none=True, **kwargs)

# Load configuration (try file first, then env, then defaults)
def load_config() -> Config:
    """Load configuration with priority: config.json > env vars > defaults"""
    config_path = os.getenv("CONFIG_PATH", "config.json")
    
    if os.path.exists(config_path):
        return Config.load_from_file(config_path)
    else:
        logger.info(f"Config file not found at {config_path}, checking environment variables")
        return Config.load_from_env()

# Global config instance
config = load_config()
