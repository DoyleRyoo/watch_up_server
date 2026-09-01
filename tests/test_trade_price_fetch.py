from decimal import Decimal

import pytest

from app.clients.upbit_trade_price import TradePriceService
from app.core.errors import AppError, ErrorCode
from app.schemas.upbit import UpbitTicker


class Source:
    def __init__(self, tickers: list[UpbitTicker]) -> None:
        self.tickers = tickers
        self.calls: list[tuple[tuple[str, ...], int | None]] = []

    async def get_tickers(
        self, market_codes: tuple[str, ...], *, max_retries: int | None = None
    ) -> list[UpbitTicker]:
        self.calls.append((market_codes, max_retries))
        return self.tickers


@pytest.mark.asyncio
async def test_trade_price_uses_one_direct_no_retry_call_and_quantizes() -> None:
    source = Source(
        [
            UpbitTicker(
                market="KRW-BTC",
                trade_price=Decimal("123.1234567890123456784"),
                signed_change_rate=0,
            )
        ]
    )
    price, quoted_at = await TradePriceService(source).fetch("KRW-BTC")
    assert price == Decimal("123.123456789012345678")
    assert quoted_at.tzinfo is not None
    assert source.calls == [(("KRW-BTC",), 0)]


@pytest.mark.asyncio
async def test_trade_price_rejects_market_mismatch() -> None:
    source = Source(
        [UpbitTicker(market="KRW-ETH", trade_price=1, signed_change_rate=0)]
    )
    with pytest.raises(AppError) as raised:
        await TradePriceService(source).fetch("KRW-BTC")
    assert raised.value.code is ErrorCode.UPBIT_UNAVAILABLE
