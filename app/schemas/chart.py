"""Public response models for the daily chart endpoint."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import PlainSerializer, model_validator

from app.models.price import decimal_to_json_number
from app.schemas.base import APIModel
from app.schemas.common import ListMeta


JsonDecimal = Annotated[
    Decimal,
    PlainSerializer(
        decimal_to_json_number,
        return_type=int | float,
        when_used="json",
    ),
]


class ChartCandleResponse(APIModel):
    date: date
    closing_price: JsonDecimal


class ChartDataResponse(APIModel):
    market_code: str
    period: Literal["30d"] = "30d"
    candles: list[ChartCandleResponse]


class ChartResponse(APIModel):
    data: ChartDataResponse
    meta: ListMeta

    @model_validator(mode="after")
    def validate_count(self) -> "ChartResponse":
        if self.meta.count != len(self.data.candles):
            raise ValueError("meta.count must match the number of candles")
        return self


__all__ = ["ChartCandleResponse", "ChartDataResponse", "ChartResponse"]
