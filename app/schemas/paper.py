from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator

from app.models.market import MarketCode
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


class TradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class TradeRequest(APIModel):
    model_config = ConfigDict(**APIModel.model_config, extra="forbid")

    market_code: MarketCode
    side: TradeSide
    amount_krw: StrictStr | None = None
    quantity: StrictStr | None = None

    @model_validator(mode="after")
    def validate_side_fields(self) -> "TradeRequest":
        if self.side is TradeSide.BUY:
            if self.amount_krw is None or self.quantity is not None:
                raise ValueError("BUY requires amountKrw only")
            if not self.amount_krw.isdigit() or self.amount_krw.startswith("0"):
                raise ValueError("amountKrw must be a positive integer string")
            if int(self.amount_krw) > BIGINT_MAX:
                raise ValueError("amountKrw exceeds BIGINT")
        else:
            if self.quantity is None or self.amount_krw is not None:
                raise ValueError("SELL requires quantity only")
            try:
                parsed = Decimal(self.quantity)
            except Exception as exc:
                raise ValueError("quantity must be a decimal string") from exc
            integer, dot, fractional = self.quantity.partition(".")
            if (
                not parsed.is_finite()
                or parsed <= 0
                or (dot and (not fractional or len(fractional) > 18))
                or not integer.isdigit()
                or "e" in self.quantity.casefold()
            ):
                raise ValueError("quantity must be a positive decimal string")
        return self


class PortfolioPriceStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    PRICE_ERROR = "PRICE_ERROR"


class ValuationStatus(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    PARTIAL = "PARTIAL"


class PaperHolding(APIModel):
    market_code: str
    korean_name: str
    english_name: str
    quantity: str
    cost_basis_krw: str
    avg_price_krw: str | None
    current_price: str | None
    price_status: PortfolioPriceStatus
    unrealized_pnl_krw: str | None
    value_krw: str | None


class PaperPortfolio(APIModel):
    cash_balance_krw: str
    holdings: list[PaperHolding]
    total_holdings_value_krw: str | None
    total_unrealized_pnl_krw: str | None
    total_realized_pnl_krw: str
    total_assets_krw: str | None
    total_pnl_krw: str | None
    total_return_rate: str | None
    valuation_status: ValuationStatus


class HistoryMeta(APIModel):
    count: int = Field(ge=0)
    has_more: bool


class PaperHistoryResponse(APIModel):
    data: list[PaperTransaction]
    meta: HistoryMeta

    @model_validator(mode="after")
    def validate_count(self) -> "PaperHistoryResponse":
        if self.meta.count != len(self.data):
            raise ValueError("meta.count must match the number of data items")
        return self


class PortfolioMeta(APIModel):
    count: int = Field(ge=0)


class PaperPortfolioResponse(APIModel):
    data: PaperPortfolio
    meta: PortfolioMeta
