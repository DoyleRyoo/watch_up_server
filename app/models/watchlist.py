"""Validated internal models for rows stored in public.watchlist."""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)


POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807
MARKET_CODE_PATTERN = r"^KRW-[A-Z0-9]+$"
WatchlistId = Annotated[
    StrictInt,
    Field(gt=0, le=POSTGRES_BIGINT_MAX),
]
MarketCode = Annotated[
    StrictStr,
    Field(min_length=5, max_length=20, pattern=MARKET_CODE_PATTERN),
]
KoreanName = Annotated[StrictStr, Field(min_length=1, max_length=100)]
EnglishName = Annotated[StrictStr, Field(min_length=1, max_length=100)]


class WatchlistStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CAUTION = "CAUTION"
    UNAVAILABLE = "UNAVAILABLE"
    PRICE_ERROR = "PRICE_ERROR"


class WatchlistRow(BaseModel):
    """One fully validated row returned by the Supabase Data API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: WatchlistId
    user_id: UUID
    market_code: MarketCode
    korean_name: KoreanName
    english_name: EnglishName
    created_at: AwareDatetime


class WatchlistInsert(BaseModel):
    """Server-owned values allowed in a watchlist INSERT payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: UUID
    market_code: MarketCode
    korean_name: KoreanName
    english_name: EnglishName

    def to_db_payload(self) -> dict[str, str]:
        return self.model_dump(mode="json")


class WatchlistItem(BaseModel):
    """One DB row combined with current market and price state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: WatchlistId
    market_code: MarketCode
    korean_name: KoreanName
    english_name: EnglishName
    symbol: StrictStr = Field(min_length=1, max_length=16)
    current_price: Decimal | None
    signed_change_rate: Decimal | None
    status: WatchlistStatus
    is_stale: StrictBool
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_state(self) -> "WatchlistItem":
        has_price = (
            self.current_price is not None and self.signed_change_rate is not None
        )
        if self.status in {WatchlistStatus.ACTIVE, WatchlistStatus.CAUTION}:
            if not has_price:
                raise ValueError("available watchlist items require price values")
        elif (
            self.current_price is not None
            or self.signed_change_rate is not None
            or self.is_stale
        ):
            raise ValueError("unavailable price states cannot carry price values")
        return self


__all__ = [
    "MARKET_CODE_PATTERN",
    "POSTGRES_BIGINT_MAX",
    "MarketCode",
    "WatchlistId",
    "WatchlistInsert",
    "WatchlistItem",
    "WatchlistRow",
    "WatchlistStatus",
]
