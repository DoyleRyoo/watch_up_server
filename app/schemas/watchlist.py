"""Public watchlist request and response schemas."""

from decimal import Decimal
from typing import Annotated

from pydantic import AwareDatetime, PlainSerializer, StrictStr

from app.models.price import decimal_to_json_number
from app.models.watchlist import WatchlistId, WatchlistStatus
from app.schemas.base import APIModel


JsonDecimal = Annotated[
    Decimal,
    PlainSerializer(
        decimal_to_json_number,
        return_type=int | float,
        when_used="json",
    ),
]


class WatchlistCreateRequest(APIModel):
    market_code: StrictStr


class WatchlistCreatedItem(APIModel):
    id: WatchlistId
    market_code: str
    korean_name: str
    english_name: str
    created_at: AwareDatetime


class WatchlistItemResponse(APIModel):
    id: WatchlistId
    market_code: str
    korean_name: str
    english_name: str
    symbol: str
    current_price: JsonDecimal | None
    signed_change_rate: JsonDecimal | None
    status: WatchlistStatus
    is_stale: bool
    created_at: AwareDatetime


__all__ = [
    "WatchlistCreateRequest",
    "WatchlistCreatedItem",
    "WatchlistItemResponse",
]
