"""Validated environment settings. No working secrets are bundled with the app."""

import math
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SCORE_FIELDS = ("idea", "plot", "screenplay", "acting", "sound", "visuals")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    app_version: str = "0.1.0"
    database_url: str = "sqlite:///./data/cinema.db"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    jwt_secret: str = Field(min_length=32, repr=False)
    access_token_ttl_minutes: int = Field(default=30, ge=1, le=1440)
    rating_weights: dict[str, float] = Field(
        default_factory=lambda: dict.fromkeys(SCORE_FIELDS, 1.0)
    )
    kinopoisk_api_key: str | None = Field(default=None, repr=False)
    external_cache_ttl_seconds: int = Field(default=86400, ge=1)
    smtp_host: str | None = None
    smtp_port: int = Field(default=1025, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = Field(default="", repr=False)
    smtp_from: str = "cinema-club@example.org"
    smtp_tls: bool = False

    @field_validator("log_level", mode="before")
    @classmethod
    def uppercase_level(cls, value):
        return value.upper() if isinstance(value, str) else value

    @field_validator("rating_weights", mode="before")
    @classmethod
    def validate_weights(cls, value):
        if not isinstance(value, dict) or set(value) != set(SCORE_FIELDS):
            raise ValueError("rating_weights must contain exactly the six score fields")
        if any(
            isinstance(weight, bool)
            or not isinstance(weight, int | float)
            or not math.isfinite(weight)
            or weight <= 0
            for weight in value.values()
        ):
            raise ValueError("rating weights must be finite positive numbers")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
