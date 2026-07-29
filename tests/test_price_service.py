import asyncio
import json
from collections.abc import Sequence
from decimal import Decimal

import pytest
from redis.exceptions import ConnectionError

from app.cache.keys import (
    PRICE_TTL_SECONDS,
    STALE_PRICE_TTL_SECONDS,
    TICKER_REFRESH_LOCK_KEY,
    price_key,
    stale_price_key,
)
from app.cache.redis import RedisCache
from app.clients.upbit import RateLimitGroup, UpbitClientResponseError
from app.core.errors import AppError, ErrorCode
from app.models.price import PriceQuote
from app.schemas.upbit import UpbitTicker
from app.services.price import PriceService
from tests.test_redis_cache import FakeRedis


BTC = UpbitTicker(
    market="KRW-BTC",
    trade_price=142_300_000,
    signed_change_rate=0.0125,
)
ETH = UpbitTicker(
    market="KRW-ETH",
    trade_price=4_321_000.25,
    signed_change_rate=-0.005,
)


class FakeTickerSource:
    def __init__(
        self,
        tickers: Sequence[UpbitTicker] = (),
        *,
        error: BaseException | None = None,
    ) -> None:
        self.tickers = list(tickers)
        self.error = error
        self.calls: list[tuple[tuple[str, ...], int | None]] = []

    async def get_tickers(
        self,
        market_codes: Sequence[str],
        *,
        max_retries: int | None = None,
    ) -> list[UpbitTicker]:
        self.calls.append((tuple(market_codes), max_retries))
        if self.error is not None:
            raise self.error
        return list(self.tickers)


class CacheAfterTickerLockRedis(FakeRedis):
    def __init__(self, values_after_lock: dict[str, str]) -> None:
        super().__init__()
        self.values_after_lock = values_after_lock

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool = False,
    ) -> bool | None:
        result = await super().set(name, value, ex=ex, nx=nx)
        if name == TICKER_REFRESH_LOCK_KEY and nx and result:
            self.values.update(self.values_after_lock)
        return result


def quote_payload(ticker: UpbitTicker, *, market_code: str | None = None) -> str:
    quote = PriceQuote.from_upbit(ticker)
    payload = quote.to_cache_value()
    if market_code is not None:
        payload["market_code"] = market_code
    return json.dumps(payload)


def put_fresh(redis: FakeRedis, ticker: UpbitTicker) -> None:
    redis.values[price_key(ticker.market)] = quote_payload(ticker)


def put_stale(redis: FakeRedis, ticker: UpbitTicker) -> None:
    redis.values[stale_price_key(ticker.market)] = quote_payload(ticker)


def make_service(
    source: FakeTickerSource,
    redis: FakeRedis,
    *,
    sleeper: object | None = None,
) -> PriceService:
    kwargs: dict[str, object] = {
        "upbit_client": source,
        "redis_cache": RedisCache(redis),
    }
    if sleeper is not None:
        kwargs["sleeper"] = sleeper
    return PriceService(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_all_fresh_hits_use_one_mget_and_no_ticker() -> None:
    redis = FakeRedis()
    put_fresh(redis, BTC)
    put_fresh(redis, ETH)
    source = FakeTickerSource()

    result = await make_service(source, redis).get_prices(["KRW-BTC", "KRW-ETH"])

    assert list(result) == ["KRW-BTC", "KRW-ETH"]
    assert result["KRW-BTC"].quote.trade_price == Decimal("142300000")
    assert all(not value.is_stale for value in result.values())
    assert source.calls == []
    assert redis.commands == [("mget", (price_key("KRW-BTC"), price_key("KRW-ETH")))]


@pytest.mark.asyncio
async def test_invalid_cache_entries_are_individual_misses_and_one_batch_refresh() -> (
    None
):
    redis = FakeRedis()
    put_fresh(redis, BTC)
    redis.values[price_key("KRW-ETH")] = json.dumps(
        {
            "market_code": "KRW-XRP",
            "trade_price": "4321000",
            "signed_change_rate": -0.005,
        }
    )
    source = FakeTickerSource([ETH])

    result = await make_service(source, redis).get_prices(
        ["KRW-BTC", "KRW-ETH", "KRW-ETH"]
    )

    assert list(result) == ["KRW-BTC", "KRW-ETH"]
    assert source.calls == [(("KRW-ETH",), None)]
    assert redis.ttls[price_key("KRW-ETH")] == PRICE_TTL_SECONDS == 5
    assert redis.ttls[stale_price_key("KRW-ETH")] == (STALE_PRICE_TTL_SECONDS) == 3600
    pipeline_writes = next(
        command[1] for command in redis.commands if command[0] == "pipeline_execute"
    )
    assert {write[0] for write in pipeline_writes} == {
        price_key("KRW-ETH"),
        stale_price_key("KRW-ETH"),
    }


@pytest.mark.asyncio
async def test_lock_owner_double_check_avoids_upbit() -> None:
    redis = CacheAfterTickerLockRedis({price_key("KRW-BTC"): quote_payload(BTC)})
    source = FakeTickerSource()

    result = await make_service(source, redis).get_prices(["KRW-BTC"])

    assert result["KRW-BTC"].quote.trade_price == BTC.trade_price
    assert source.calls == []
    assert [command[0] for command in redis.commands] == [
        "mget",
        "set",
        "mget",
        "eval",
    ]


@pytest.mark.asyncio
async def test_lock_loser_waits_for_complete_cache_and_never_calls_upbit() -> None:
    redis = FakeRedis()
    redis.values[TICKER_REFRESH_LOCK_KEY] = "other-owner"
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            put_fresh(redis, BTC)
            put_fresh(redis, ETH)

    source = FakeTickerSource()
    result = await make_service(source, redis, sleeper=sleeper).get_prices(
        ["KRW-BTC", "KRW-ETH"]
    )

    assert set(result) == {"KRW-BTC", "KRW-ETH"}
    assert sleeps == [0.1, 0.1, 0.1]
    assert source.calls == []
    assert redis.values[TICKER_REFRESH_LOCK_KEY] == "other-owner"


@pytest.mark.asyncio
async def test_lock_loser_stops_after_five_rechecks() -> None:
    redis = FakeRedis()
    redis.values[TICKER_REFRESH_LOCK_KEY] = "other-owner"
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(AppError) as captured:
        await make_service(FakeTickerSource(), redis, sleeper=sleeper).get_prices(
            ["KRW-BTC"]
        )

    assert captured.value.code is ErrorCode.CACHE_REFRESH_IN_PROGRESS
    assert sleeps == [0.1] * 5
    assert redis.values[TICKER_REFRESH_LOCK_KEY] == "other-owner"


@pytest.mark.asyncio
async def test_418_uses_only_complete_stale_set_and_marks_it() -> None:
    redis = FakeRedis()
    put_fresh(redis, BTC)
    put_stale(redis, ETH)
    blocked = AppError(
        code=ErrorCode.UPBIT_TEMPORARILY_BLOCKED,
        message="safe blocked",
    )
    source = FakeTickerSource(error=blocked)

    result = await make_service(source, redis).get_prices(["KRW-BTC", "KRW-ETH"])

    assert result["KRW-BTC"].is_stale is False
    assert result["KRW-ETH"].is_stale is True
    assert source.calls == [(("KRW-ETH",), None)]
    assert not any(command[0] == "pipeline" for command in redis.commands)


@pytest.mark.asyncio
async def test_418_with_missing_or_mismatched_stale_preserves_blocked_error() -> None:
    for stale_payload in (
        None,
        quote_payload(ETH, market_code="KRW-XRP"),
        json.dumps({"market_code": "KRW-ETH", "trade_price": "1"}),
    ):
        redis = FakeRedis()
        if stale_payload is not None:
            redis.values[stale_price_key("KRW-ETH")] = stale_payload
        blocked = AppError(
            code=ErrorCode.UPBIT_TEMPORARILY_BLOCKED,
            message="safe blocked",
        )

        with pytest.raises(AppError) as captured:
            await make_service(
                FakeTickerSource(error=blocked),
                redis,
            ).get_prices(["KRW-ETH"])

        assert captured.value.code is ErrorCode.UPBIT_TEMPORARILY_BLOCKED


@pytest.mark.asyncio
async def test_redis_failure_uses_one_no_retry_batch_without_fake_lock() -> None:
    redis = FakeRedis()
    redis.failures["mget"] = ConnectionError("private Redis host")
    source = FakeTickerSource([BTC, ETH])

    result = await make_service(source, redis).get_prices(["KRW-BTC", "KRW-ETH"])

    assert set(result) == {"KRW-BTC", "KRW-ETH"}
    assert source.calls == [(("KRW-BTC", "KRW-ETH"), 0)]
    assert all(command[0] not in {"set", "pipeline"} for command in redis.commands)


@pytest.mark.asyncio
async def test_redis_direct_fallback_preserves_upbit_error() -> None:
    redis = FakeRedis()
    redis.failures["mget"] = ConnectionError("private Redis host")
    failure = AppError(
        code=ErrorCode.UPBIT_RATE_LIMITED,
        message="safe rate limit",
    )
    source = FakeTickerSource(error=failure)

    with pytest.raises(AppError) as captured:
        await make_service(source, redis).get_prices(["KRW-BTC", "KRW-ETH"])

    assert captured.value is failure
    assert source.calls == [(("KRW-BTC", "KRW-ETH"), 0)]


@pytest.mark.asyncio
async def test_cache_write_failure_keeps_valid_ticker_result() -> None:
    redis = FakeRedis()
    redis.failures["pipeline_execute"] = ConnectionError("private pipeline host")
    source = FakeTickerSource([BTC])

    result = await make_service(source, redis).get_prices(["KRW-BTC"])

    assert result["KRW-BTC"].quote.trade_price == BTC.trade_price
    assert result["KRW-BTC"].is_stale is False


@pytest.mark.asyncio
async def test_partial_ticker_and_unrequested_extra_are_not_fabricated_or_cached() -> (
    None
):
    xrp = UpbitTicker(
        market="KRW-XRP",
        trade_price=1000,
        signed_change_rate=0.1,
    )
    redis = FakeRedis()
    source = FakeTickerSource([BTC, xrp])

    result = await make_service(source, redis).get_prices(["KRW-BTC", "KRW-ETH"])

    assert set(result) == {"KRW-BTC"}
    assert source.calls == [(("KRW-BTC", "KRW-ETH"), None)]
    assert price_key("KRW-XRP") not in redis.values
    assert price_key("KRW-ETH") not in redis.values


@pytest.mark.asyncio
async def test_non_retryable_4xx_maps_to_safe_upbit_unavailable() -> None:
    error = UpbitClientResponseError(
        status_code=404,
        group=RateLimitGroup.TICKER,
    )

    with pytest.raises(AppError) as captured:
        await make_service(FakeTickerSource(error=error), FakeRedis()).get_prices(
            ["KRW-BTC"]
        )

    assert captured.value.code is ErrorCode.UPBIT_UNAVAILABLE
    assert captured.value.status_code == 502


@pytest.mark.asyncio
async def test_cancellation_propagates_and_releases_owned_lock() -> None:
    redis = FakeRedis()

    with pytest.raises(asyncio.CancelledError):
        await make_service(
            FakeTickerSource(error=asyncio.CancelledError()),
            redis,
        ).get_prices(["KRW-BTC"])

    assert TICKER_REFRESH_LOCK_KEY not in redis.values
    assert any(command[0] == "eval" for command in redis.commands)
