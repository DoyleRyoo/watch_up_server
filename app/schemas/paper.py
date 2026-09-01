from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StrictStr, field_validator

from app.schemas.base import APIModel

INITIAL_GRANT_KRW = 1_000_000
TOP_UP_MIN_KRW = 1
TOP_UP_MAX_KRW = 2_100_000_000
TOP_UP_LIFETIME_CAP_KRW = 100_000_000_000
BIGINT_MAX = 9_223_372_036_854_775_807


class PaperTransactionType(StrEnum):
    INITIAL_GRANT = "INITIAL_GRANT"
    TOP_UP = "TOP_UP"
    BUY = "BUY"
    SELL = "SELL"


class PaperAssetClass(StrEnum):
    CRYPTO = "CRYPTO"


class PaperAccount(APIModel):
    cash_balance_krw: str
    lifetime_top_up_krw: str
    top_up_min_krw: str = "1"
    top_up_max_krw: str = "2100000000"
    top_up_lifetime_cap_krw: str = "100000000000"


class PaperTransaction(APIModel):
    id: str
    type: PaperTransactionType
    asset_class: PaperAssetClass | None
    market_code: str | None
    execution_price: str | None
    quantity: str | None
    cash_delta_krw: str
    balance_after_krw: str
    disposed_cost_basis_krw: str | None
    realized_pnl_krw: str | None
    quoted_at: datetime | None
    created_at: datetime


class TopUpRequest(APIModel):
    model_config = ConfigDict(**APIModel.model_config, extra="forbid")
    amount_krw: Annotated[StrictStr, Field(pattern=r"^[1-9][0-9]*$", max_length=19)]

    @field_validator("amount_krw")
    @classmethod
    def validate_bigint_range(cls, value: str) -> str:
        if int(value) > BIGINT_MAX:
            raise ValueError("amount_krw exceeds BIGINT")
        return value
