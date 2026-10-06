from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ExchangeName = Literal["binance_spot", "mexc_futures"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", allow_inf_nan=False)

    bot_token: str = Field(alias="BOT_TOKEN")
    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")
    bot_timezone: str = Field(default="Europe/Moscow", alias="BOT_TIMEZONE")
    bot_version: str = Field(default="0025", alias="BOT_VERSION")

    default_top_n: int = Field(default=100, alias="DEFAULT_TOP_N")
    default_interval_minutes: int = Field(default=60, alias="DEFAULT_INTERVAL_MINUTES")
    default_exchange: ExchangeName = Field(default="binance_spot", alias="DEFAULT_EXCHANGE")

    min_rating: float = Field(default=7.5, alias="MIN_RATING", ge=0, le=10)
    lookback_1h_days: int = Field(default=120, alias="LOOKBACK_1H_DAYS", gt=0)
    lookback_1d_days: int = Field(default=365, alias="LOOKBACK_1D_DAYS", gt=0)
    http_concurrency: int = Field(default=10, alias="HTTP_CONCURRENCY", gt=0)
    http_timeout_seconds: float = Field(default=25.0, alias="HTTP_TIMEOUT_SECONDS", gt=0)
    api_retry_attempts: int = Field(default=5, alias="API_RETRY_ATTEMPTS")
    api_retry_base_delay_seconds: float = Field(default=0.5, alias="API_RETRY_BASE_DELAY_SECONDS", ge=0)
    telegram_retry_attempts: int = Field(default=5, alias="TELEGRAM_RETRY_ATTEMPTS")
    telegram_retry_base_delay_seconds: float = Field(default=0.75, alias="TELEGRAM_RETRY_BASE_DELAY_SECONDS", ge=0)
    search_min_success_fraction: float = Field(default=0.80, alias="SEARCH_MIN_SUCCESS_FRACTION")
    action_cooldown_seconds: int = Field(default=60, alias="ACTION_COOLDOWN_SECONDS", ge=0)

    # /walk is a separate diagnostic and never changes Search/Tracking state.
    walk_history_days: int = Field(default=180, alias="WALK_HISTORY_DAYS", gt=0)
    walk_horizon_days: int = Field(default=30, alias="WALK_HORIZON_DAYS", gt=0)

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("default_top_n")
    @classmethod
    def validate_top_n(cls, v: int) -> int:
        if v not in {100, 200, 300}:
            raise ValueError("DEFAULT_TOP_N must be 100, 200 or 300")
        return v

    @field_validator("default_interval_minutes")
    @classmethod
    def validate_interval(cls, v: int) -> int:
        if v not in {30, 60, 240, 720}:
            raise ValueError("DEFAULT_INTERVAL_MINUTES must be 30, 60, 240 or 720")
        return v

    @field_validator("search_min_success_fraction")
    @classmethod
    def validate_success_fraction(cls, v: float) -> float:
        if not 0.5 <= v <= 1.0:
            raise ValueError("SEARCH_MIN_SUCCESS_FRACTION must be between 0.5 and 1.0")
        return v

    @field_validator("api_retry_attempts", "telegram_retry_attempts")
    @classmethod
    def validate_positive_int(cls, v: int) -> int:
        if v < 1:
            raise ValueError("retry values must be >= 1")
        return v

    @property
    def db_path(self) -> Path:
        return self.data_dir / "senior_wave_bot.sqlite3"
