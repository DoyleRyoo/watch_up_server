"""Validated internal market-list domain models."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator


class MarketStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CAUTION = "CAUTION"


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


__all__ = ["Market", "MarketStatus"]
