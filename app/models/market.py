"""Validated internal market-list domain models."""

from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


MARKET_CODE_PATTERN: Final = r"^KRW-[A-Z0-9]+$"
MarketCode = Annotated[
    StrictStr,
    Field(min_length=5, max_length=20, pattern=MARKET_CODE_PATTERN),
]


class MarketStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CAUTION = "CAUTION"
    UNAVAILABLE = "UNAVAILABLE"


class Market(BaseModel):
    """Immutable KRW market snapshot used by cache and business services."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market_code: StrictStr
    korean_name: StrictStr
    english_name: StrictStr
    status: MarketStatus

    @field_validator("market_code")
    @classmethod
    def validate_market_code(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("KRW-") or len(normalized) <= len("KRW-"):
            raise ValueError("market_code must be a non-empty KRW market")
        return normalized

    @field_validator("korean_name", "english_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("market names must not be empty")
        return normalized

    def to_cache_value(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


__all__ = ["MARKET_CODE_PATTERN", "Market", "MarketCode", "MarketStatus"]
