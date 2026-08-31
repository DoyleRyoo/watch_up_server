"""Public response models for the daily chart endpoint."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import PlainSerializer, model_validator

from app.models.market import MarketStatus
from app.schemas.base import APIModel
from app.schemas.common import ListMeta
from app.services.price import PriceStatus


JsonDecimalString = Annotated[
    Decimal,
    PlainSerializer(
        lambda value: format(value, "f"),
        return_type=str,
        when_used="json",
    ),
]


class ChartCandleResponse(APIModel):
    date: date
    closing_price: JsonDecimalString


class ChartDataResponse(APIModel):
    market_code: str
    korean_name: str
    english_name: str
    market_status: MarketStatus
    current_price: JsonDecimalString | None
    price_status: PriceStatus
    period: Literal["30d"] = "30d"
    candles: list[ChartCandleResponse]

    @model_validator(mode="after")
    def validate_price_status(self) -> "ChartDataResponse":
        has_price_error = self.price_status is PriceStatus.PRICE_ERROR
        if has_price_error != (self.current_price is None):
            raise ValueError(
                "current_price must be null if and only if price_status is PRICE_ERROR"
            )
        return self


class ChartResponse(APIModel):
    data: ChartDataResponse
    meta: ListMeta

    @model_validator(mode="after")
    def validate_count(self) -> "ChartResponse":
        if self.meta.count != len(self.data.candles):
            raise ValueError("meta.count must match the number of candles")
        return self


__all__ = ["ChartCandleResponse", "ChartDataResponse", "ChartResponse"]
