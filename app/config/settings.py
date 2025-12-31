from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from typing import Optional

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
    js_runtime: Optional[str] = Field(default=None, description="JS runtime path")
    auto_detect_runtime: bool = Field(default=True, description="Auto-detect JS runtime")
    default_format: str = Field(default="best", description="Default video format")
    default_audio_format: str = Field(default="opus", description="Default audio format")
    enable_live_streams: bool = Field(default=False, description="Allow live stream downloads")

class LoggingConfig(BaseModel):
    level: str = Field(default="INFO", description="Log level")
    format: str = Field(default="%(message)s", description="Log format")
    enable_rich: bool = Field(default=True, description="Enable rich console logging")

class I18nConfig(BaseModel):
    default_locale: str = Field(default="en", description="Default locale")
    supported_locales: list = Field(default=["en", "ja"], description="Supported locales")

class ApiConfig(BaseModel):
    title: str = Field(default="yt-dlp Streaming API", description="API title")
    description: str = Field(default="Production-ready yt-dlp streaming API", description="API description")
    version: str = Field(default="4.0.0", description="API version")
    cors_origins: list = Field(default=["*"], description="CORS allowed origins")
    debug: bool = Field(default=False, description="Enable debug mode")

class Config(BaseModel):
    redis: RedisConfig = Field(default_factory=RedisConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    ytdlp: YtDlpConfig = Field(default_factory=YtDlpConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    i18n: I18nConfig = Field(default_factory=I18nConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)

config = Config()