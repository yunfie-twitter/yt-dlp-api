import os
import json
import logging
from pydantic import BaseModel, Field
from typing import Optional, List

class RedisConfig(BaseModel):
    url: str = Field(default="redis://redis:6379", description="Redis connection URL")
    socket_timeout: int = Field(default=5, description="Redis socket timeout in seconds")

class AuthConfig(BaseModel):
    api_key_enabled: bool = Field(default=False, description="Enable API Key authentication")
    issue_allowed_origins: List[str] = Field(default=["*"], description="Origins allowed to issue API keys")

class RateLimitConfig(BaseModel):
    enabled: bool = Field(default=True, description="Enable rate limiting")
    max_requests: int = Field(default=5, ge=1, description="Max requests per window")
    window_seconds: int = Field(default=60, ge=1, description="Rate limit window in seconds")

class DownloadConfig(BaseModel):
    max_concurrent: int = Field(default=10, ge=1, le=100, description="Max concurrent downloads")
    timeout_seconds: int = Field(default=3600, ge=60, description="Download timeout in seconds")
    socket_timeout: int = Field(default=10, ge=1, description="Socket timeout for yt-dlp")
    retries: int = Field(default=3, ge=0, description="Number of retries for failed downloads")
    use_aria2c: bool = Field(default=True, description="Use aria2c for faster downloads")
    aria2c_max_connections: int = Field(default=16, ge=1, description="aria2c max connections per server")

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
    enable_gpu: bool = Field(default=False, description="Enable GPU acceleration for conversion (NVENC)")

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
    debug: bool = Field(default=True, description="Enable debug mode")

class Config(BaseModel):
    redis: RedisConfig = Field(default_factory=RedisConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    ytdlp: YtDlpConfig = Field(default_factory=YtDlpConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    i18n: I18nConfig = Field(default_factory=I18nConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)

    def save_to_file(self, path: str):
        try:
            with open(path, 'w') as f:
                f.write(self.model_dump_json(indent=2))
        except OSError as e:
            logging.error(f"Failed to save config to {path}: {e}")

    @classmethod
    def load_from_file(cls, path: str) -> "Config":
        # Load from env vars (Priority 1)
        env_config = {
            "redis": {
                "url": os.getenv("REDIS_URL", "redis://redis:6379")
            }
        }
        
        if not os.path.exists(path):
            cfg = cls()
            # Apply env overrides
            cfg_dict = cfg.model_dump()
            
            def deep_update(target, source):
                for k, v in source.items():
                    if isinstance(v, dict) and k in target and isinstance(target[k], dict):
                        deep_update(target[k], v)
                    else:
                        target[k] = v
                return target
                
            deep_update(cfg_dict, env_config)
            
            logging.info(f"Config file not found at {path}, creating default.")
            new_cfg = cls.model_validate(cfg_dict)
            new_cfg.save_to_file(path)
            return new_cfg
            
        try:
            with open(path, 'r') as f:
                content = f.read()
                if not content.strip():
                     return cls()
                data = json.loads(content)
                default_instance = cls()
                default_dict = default_instance.model_dump()
                
                def deep_update(target, source):
                    for k, v in source.items():
                        if isinstance(v, dict) and k in target and isinstance(target[k], dict):
                            deep_update(target[k], v)
                        else:
                            target[k] = v
                    return target

                deep_update(default_dict, data)
                # Apply env overrides LAST to ensure secrets from ENV are used
                deep_update(default_dict, env_config)
                
                return cls.model_validate(default_dict)
                
        except (json.JSONDecodeError, OSError) as e:
            logging.error(f"Error loading config from {path}: {e}. Using defaults.")
            return cls()

CONFIG_PATH = os.getenv("CONFIG_PATH", "/app/config.json")
config = Config.load_from_file(CONFIG_PATH)
