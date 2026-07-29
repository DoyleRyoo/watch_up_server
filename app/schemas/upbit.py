import math
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator


class _UpbitModel(BaseModel):
    """Internal Upbit response model; public REST aliases do not apply here."""

    model_config = ConfigDict(extra="ignore")


class UpbitMarket(_UpbitModel):
    market: StrictStr
    korean_name: StrictStr
    english_name: StrictStr
    market_warning: StrictStr


class _UpbitPriceModel(_UpbitModel):
    trade_price: Decimal

    @field_validator("trade_price", mode="before")
    @classmethod
    def validate_trade_price(cls, value: Any) -> Decimal:
        return _validate_number(value)


class UpbitTicker(_UpbitPriceModel):
    market: StrictStr
    signed_change_rate: Decimal

    @field_validator("signed_change_rate", mode="before")
    @classmethod
    def validate_signed_change_rate(cls, value: Any) -> Decimal:
        return _validate_number(value)


class UpbitDayCandle(_UpbitPriceModel):
    candle_date_time_kst: StrictStr

    @field_validator("candle_date_time_kst")
    @classmethod
    def validate_candle_date_time_kst(cls, value: str) -> str:
        if len(value) < 19 or value[10] != "T":
            raise ValueError("candle_date_time_kst must be ISO 8601 datetime")
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("candle_date_time_kst must be ISO 8601") from exc
        return value


def _validate_number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("value must be a JSON number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("value must be finite")

    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("value must be finite")
    return parsed
