import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_cors_origins_are_split_trimmed_and_empty_values_removed() -> None:
    settings = Settings(
        _env_file=None,
        cors_allowed_origins=(
            " http://localhost:5173, ,https://watchup.example.com,"
            "http://localhost:5173 "
        ),
    )

    assert settings.cors_allowed_origins == [
        "http://localhost:5173",
        "https://watchup.example.com",
    ]


def test_wildcard_cors_origin_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not contain"):
        Settings(_env_file=None, cors_allowed_origins="*")


def test_numeric_settings_are_converted() -> None:
    settings = Settings(
        _env_file=None,
        upbit_timeout_seconds="5.5",
        upbit_max_retries="2",
    )

    assert settings.upbit_timeout_seconds == 5.5
    assert settings.upbit_max_retries == 2
