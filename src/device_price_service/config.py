from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    live_crawl_enabled: bool = False

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3307
    mysql_database: str = "device_price"
    mysql_user: str = "device_price"
    mysql_password: str = "device-price-local"

    crawler_user_agent: str = Field(default="DevicePriceTestBot/1.0", min_length=8, max_length=256)
    http_concurrency_per_domain: int = Field(default=2, ge=1, le=10)
    http_min_delay_seconds: float = Field(default=0.8, ge=0)
    http_max_delay_seconds: float = Field(default=2.0, ge=0)
    http_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    http_read_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    http_max_redirects: int = Field(default=5, ge=0, le=10)
    http_max_response_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    browser_timeout_ms: int = Field(default=30_000, ge=1_000, le=120_000)
    browser_render_settle_ms: int = Field(default=3_000, ge=0, le=15_000)
    price_change_confirm_threshold: float = Field(default=0.30, gt=0, le=1)
    missing_confirmation_runs: int = Field(default=3, ge=2, le=10)
    discovery_count_floor_ratio: float = Field(default=0.50, ge=0.10, le=1)
    raw_storage_path: Path = Path("var/raw")
    raw_retention_days: int = Field(default=30, ge=1)
    full_crawl_interval_hours: int = Field(default=6, ge=1, le=24)
    crawl_stale_after_minutes: int = Field(default=120, ge=30, le=1440)

    @model_validator(mode="after")
    def validate_delay_range(self) -> Settings:
        if self.http_max_delay_seconds < self.http_min_delay_seconds:
            raise ValueError("HTTP_MAX_DELAY_SECONDS must be >= HTTP_MIN_DELAY_SECONDS")
        return self

    @property
    def sqlalchemy_url(self) -> str:
        user = quote_plus(self.mysql_user)
        password = quote_plus(self.mysql_password)
        return (
            f"mysql+pymysql://{user}:{password}@{self.mysql_host}:{self.mysql_port}/"
            f"{self.mysql_database}?charset=utf8mb4"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
