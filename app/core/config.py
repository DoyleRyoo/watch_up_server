from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
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

    database_session_url: str = ""
    database_role: str = "authenticated"
    database_pool_min_size: int = Field(default=1, ge=1)
    database_pool_max_size: int = Field(default=5, ge=1)

    redis_url: str = ""
    redis_timeout_seconds: float = Field(default=2, gt=0)

    upbit_base_url: str = "https://api.upbit.com"
    upbit_timeout_seconds: float = Field(default=5, gt=0)
    upbit_max_retries: int = Field(default=2, ge=0)

    cors_allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("database_session_url")
    @classmethod
    def validate_database_session_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return normalized
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            raise ValueError("DATABASE_SESSION_URL must be a PostgreSQL URL")
        return normalized

    @field_validator("database_role")
    @classmethod
    def validate_database_role(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or not normalized.replace("_", "a").isalnum():
            raise ValueError("DATABASE_ROLE must be a PostgreSQL identifier")
        return normalized

    @model_validator(mode="after")
    def validate_database_pool_sizes(self) -> "Settings":
        if self.database_pool_min_size > self.database_pool_max_size:
            raise ValueError("DATABASE_POOL_MIN_SIZE must not exceed max size")
        return self

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
        for origin in origins:
            parsed = urlsplit(origin)
            if "*" in origin:
                raise ValueError("CORS_ALLOWED_ORIGINS must not contain '*'")
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError(
                    "CORS_ALLOWED_ORIGINS entries must be exact HTTP(S) origins"
                )

        return list(dict.fromkeys(origins))


@lru_cache
def get_settings() -> Settings:
    return Settings()
