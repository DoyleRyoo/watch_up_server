"""Validated internal models for daily chart cache snapshots."""

import math
from datetime import date as Date
from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.price import decimal_to_json_number
from app.models.watchlist import MarketCode


CHART_RESPONSE_PERIOD: Final[Literal["30d"]] = "30d"


class ChartCandle(BaseModel):
    """One validated daily closing price."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: Date
    closing_price: Decimal = Field(ge=0)

    @field_validator("date", mode="before")
    @classmethod
    def validate_date(cls, value: Any) -> Date:
        if isinstance(value, Date):
            return value
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("chart candle date must be YYYY-MM-DD")
        try:
            parsed = Date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("chart candle date must be YYYY-MM-DD") from exc
        if parsed.isoformat() != value:
            raise ValueError("chart candle date must be YYYY-MM-DD")
        return parsed

    @field_validator("closing_price", mode="before")
    @classmethod
    def validate_closing_price(cls, value: Any) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("closing_price must be a JSON number")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("closing_price must be finite")
        parsed = Decimal(str(value))
        if not parsed.is_finite():
            raise ValueError("closing_price must be finite")
        return parsed

    def to_cache_value(self) -> dict[str, str | int | float]:
        return {
            "date": self.date.isoformat(),
            "closing_price": decimal_to_json_number(self.closing_price),
        }


class ChartSnapshot(BaseModel):
    """A market-bound, cacheable 30-day chart snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market_code: MarketCode
    period: Literal["30d"] = CHART_RESPONSE_PERIOD
    candles: tuple[ChartCandle, ...] = Field(max_length=30)

    @model_validator(mode="after")
    def validate_ascending_dates(self) -> "ChartSnapshot":
        dates = tuple(candle.date for candle in self.candles)
        if dates != tuple(sorted(dates)):
            raise ValueError("chart candles must be sorted by date")
        return self

    def to_cache_value(self) -> dict[str, object]:
        return {
            "market_code": self.market_code,
            "period": self.period,
            "candles": [candle.to_cache_value() for candle in self.candles],
        }


__all__ = ["CHART_RESPONSE_PERIOD", "ChartCandle", "ChartSnapshot"]
