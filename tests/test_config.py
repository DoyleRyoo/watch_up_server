import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_cors_origins_are_split_trimmed_and_empty_values_removed() -> None:
    settings = Settings(
        _env_file=None,
        cors_allowed_origins=(
            " http://localhost:8080, ,https://watchup.example.com,"
            "http://localhost:8080 "
        ),
    )

    assert settings.cors_allowed_origins == [
        "http://localhost:8080",
        "https://watchup.example.com",
    ]


def test_wildcard_cors_origin_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not contain"):
        Settings(_env_file=None, cors_allowed_origins="*")


def test_numeric_settings_are_converted() -> None:
    settings = Settings(
        _env_file=None,
        redis_timeout_seconds="1.25",
        upbit_timeout_seconds="5.5",
        upbit_max_retries="2",
    )

    assert settings.redis_timeout_seconds == 1.25
    assert settings.upbit_timeout_seconds == 5.5
    assert settings.upbit_max_retries == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"redis_timeout_seconds": 0},
        {"redis_timeout_seconds": -1},
        {"upbit_timeout_seconds": 0},
        {"upbit_timeout_seconds": -1},
        {"upbit_max_retries": -1},
        {"upbit_base_url": ""},
        {"upbit_base_url": "https://"},
        {"upbit_base_url": "ftp://api.upbit.com"},
    ],
)
def test_invalid_upbit_settings_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)
