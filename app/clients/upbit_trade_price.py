from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Protocol

from app.clients.upbit import UPBIT_ERROR_MESSAGE, UpbitClientResponseError
from app.core.errors import AppError, ErrorCode
from app.schemas.upbit import UpbitTicker

NUMERIC_QUANTUM = Decimal("0.000000000000000001")


class TradeTickerSource(Protocol):
    async def get_tickers(
        self, market_codes: Sequence[str], *, max_retries: int | None = None
    ) -> list[UpbitTicker]: ...


class TradePriceService:
    def __init__(self, source: TradeTickerSource) -> None:
        self._source = source

    async def fetch(self, market_code: str) -> tuple[Decimal, datetime]:
        try:
            tickers = await self._source.get_tickers((market_code,), max_retries=0)
        except UpbitClientResponseError:
            raise AppError(
                code=ErrorCode.UPBIT_UNAVAILABLE, message=UPBIT_ERROR_MESSAGE
            ) from None
        if len(tickers) != 1 or tickers[0].market != market_code:
            raise AppError(
                code=ErrorCode.UPBIT_UNAVAILABLE, message=UPBIT_ERROR_MESSAGE
            )
        with localcontext() as context:
            context.prec = 80
            price = tickers[0].trade_price.quantize(
                NUMERIC_QUANTUM, rounding=ROUND_HALF_EVEN
            )
        if not price.is_finite() or price <= 0:
            raise AppError(
                code=ErrorCode.UPBIT_UNAVAILABLE, message=UPBIT_ERROR_MESSAGE
            )
        return price, datetime.now(UTC)
