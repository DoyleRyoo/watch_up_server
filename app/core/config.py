from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        enable_decoding=False,
    )

    app_env: str = "development"

    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_jwks_url: str = ""
    supabase_issuer: str = ""
    supabase_audience: str = "authenticated"

    redis_url: str = ""

    upbit_base_url: str = "https://api.upbit.com"
    upbit_timeout_seconds: float = Field(default=5, gt=0)
    upbit_max_retries: int = Field(default=2, ge=0)

    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    @field_validator("upbit_base_url")
    @classmethod
    def validate_upbit_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("UPBIT_BASE_URL must be an HTTP(S) URL")
        return normalized

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors_allowed_origins(cls, value: Any) -> list[str]:
        candidates: list[Any]
        if isinstance(value, str):
            candidates = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            candidates = list(value)
        else:
            raise ValueError("CORS_ALLOWED_ORIGINS must be a comma-separated string")

        origins = [str(origin).strip() for origin in candidates if str(origin).strip()]
        if "*" in origins:
            raise ValueError("CORS_ALLOWED_ORIGINS must not contain '*'")

        return list(dict.fromkeys(origins))


@lru_cache
def get_settings() -> Settings:
    return Settings()
