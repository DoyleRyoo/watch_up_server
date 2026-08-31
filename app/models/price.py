"""Validated internal price-cache and ticker result models."""

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.market import MarketCode
from app.schemas.upbit import UpbitTicker


class PriceQuote(BaseModel):
    """One validated price shared by cache and business services."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market_code: MarketCode
    trade_price: Decimal = Field(gt=0)
    signed_change_rate: Decimal

    @field_validator("trade_price", "signed_change_rate", mode="before")
    @classmethod
    def validate_number(cls, value: Any) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("price values must be JSON numbers")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("price values must be finite")
        parsed = Decimal(str(value))
        if not parsed.is_finite():
            raise ValueError("price values must be finite")
        return parsed

    @classmethod
    def from_upbit(cls, ticker: UpbitTicker) -> "PriceQuote":
        return cls(
            market_code=ticker.market,
            trade_price=ticker.trade_price,
            signed_change_rate=ticker.signed_change_rate,
        )

    def to_cache_value(self) -> dict[str, str | int | float]:
        return {
            "market_code": self.market_code,
            "trade_price": decimal_to_json_number(self.trade_price),
            "signed_change_rate": decimal_to_json_number(self.signed_change_rate),
        }


@dataclass(frozen=True, slots=True)
class ResolvedPrice:
    quote: PriceQuote
    is_stale: bool


def decimal_to_json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


__all__ = [
    "PriceQuote",
    "ResolvedPrice",
    "decimal_to_json_number",
]
