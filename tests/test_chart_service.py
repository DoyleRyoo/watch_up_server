import asyncio
import json
from collections.abc import Sequence
from decimal import Decimal

import pytest
from redis.exceptions import ConnectionError

from app.cache.keys import (
    CHART_TTL_SECONDS,
    STALE_CHART_TTL_SECONDS,
    chart_key,
    stale_chart_key,
)
from app.cache.redis import RedisCache
from app.clients.upbit import RateLimitGroup, UpbitClientResponseError
from app.core.errors import AppError, ErrorCode
from app.models.chart import ChartCandle, ChartSnapshot
from app.models.market import Market, MarketStatus
from app.schemas.upbit import UpbitDayCandle
from app.services.chart import ChartService
from tests.test_redis_cache import FakeRedis


BTC = Market(
    market_code="KRW-BTC",
    korean_name="비트코인",
    english_name="Bitcoin",
    status=MarketStatus.ACTIVE,
)
CAUTION_BTC = BTC.model_copy(update={"status": MarketStatus.CAUTION})


def candle(day: int, price: int | float) -> UpbitDayCandle:
    return UpbitDayCandle(
        candle_date_time_kst=f"2026-06-{day:02d}T09:00:00",
        trade_price=price,
    )


class FakeMarketListService:
    def __init__(
        self,
        market: Market | None = BTC,
        *,
        error: BaseException | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.market = market
        self.error = error
        self.calls: list[str] = []
        self.events = events

    async def get_market_by_code(self, market_code: str) -> Market | None:
        self.calls.append(market_code)
        if self.events is not None:
            self.events.append("market")
        if self.error is not None:
            raise self.error
        if self.market is None or self.market.market_code != market_code:
            return None
        return self.market


class FakeCandleSource:
    def __init__(
        self,
        candles: Sequence[UpbitDayCandle] = (),
        *,
        error: BaseException | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.candles = list(candles)
        self.error = error
        self.calls: list[tuple[str, int | None]] = []
        self.events = events

    async def get_day_candles(
        self,
        market_code: str,
        *,
        max_retries: int | None = None,
    ) -> list[UpbitDayCandle]:
        self.calls.append((market_code, max_retries))
        if self.events is not None:
            self.events.append("upbit")
        if self.error is not None:
            raise self.error
        return list(self.candles)


def make_service(
    source: FakeCandleSource,
    redis: FakeRedis,
    market_service: FakeMarketListService | None = None,
) -> ChartService:
    return ChartService(
        upbit_client=source,
        redis_cache=RedisCache(redis),
        market_list_service=market_service or FakeMarketListService(),  # type: ignore[arg-type]
    )


def snapshot_payload(
    *,
    market_code: str = "KRW-BTC",
    period: str = "30d",
    candles: object | None = None,
) -> str:
    payload = {
        "market_code": market_code,
        "period": period,
        "candles": [] if candles is None else candles,
    }
    return json.dumps(payload)


@pytest.mark.parametrize(
    "market_code",
    [
        "BTC",
        "USD-BTC",
        "krw-btc",
        "KRW-",
        " KRW-BTC",
        "KRW-BTC ",
        "KRW-BTC!",
        "KRW-ABCDEFGHIJKLMNOPQ",
    ],
)
@pytest.mark.asyncio
async def test_invalid_format_stops_before_market_cache_and_upbit(
    market_code: str,
) -> None:
    redis = FakeRedis()
    market_service = FakeMarketListService()
    source = FakeCandleSource()

    with pytest.raises(AppError) as captured:
        await make_service(source, redis, market_service).get_chart(market_code)

    assert captured.value.code is ErrorCode.INVALID_MARKET_CODE
    assert market_service.calls == []
    assert redis.commands == []
    assert source.calls == []


@pytest.mark.asyncio
async def test_missing_exact_market_stops_before_cache_and_upbit() -> None:
    redis = FakeRedis()
    market_service = FakeMarketListService(None)
    source = FakeCandleSource()

    with pytest.raises(AppError) as captured:
        await make_service(source, redis, market_service).get_chart("KRW-BTC")

    assert captured.value.code is ErrorCode.INVALID_MARKET_CODE
    assert market_service.calls == ["KRW-BTC"]
    assert redis.commands == []
    assert source.calls == []


@pytest.mark.asyncio
async def test_market_list_error_is_preserved_and_stops_later_steps() -> None:
    failure = AppError(
        code=ErrorCode.CACHE_REFRESH_IN_PROGRESS,
        message="safe market refresh error",
    )
    market_service = FakeMarketListService(error=failure)
    redis = FakeRedis()
    source = FakeCandleSource()

    with pytest.raises(AppError) as captured:
        await make_service(source, redis, market_service).get_chart("KRW-BTC")

    assert captured.value is failure
    assert redis.commands == []
    assert source.calls == []


@pytest.mark.parametrize("market", [BTC, CAUTION_BTC])
@pytest.mark.asyncio
async def test_active_and_caution_markets_use_valid_fresh_empty_cache(
    market: Market,
) -> None:
    redis = FakeRedis()
    redis.values[chart_key("KRW-BTC")] = snapshot_payload()
    source = FakeCandleSource()

    result = await make_service(
        source,
        redis,
        FakeMarketListService(market),
    ).get_chart("KRW-BTC")

    assert result.candles == ()
    assert source.calls == []
    assert redis.commands == [("get", "chart:KRW-BTC:1d")]


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        snapshot_payload(market_code="KRW-ETH"),
        snapshot_payload(period="1d"),
        snapshot_payload(candles=[{"date": "bad", "closing_price": 1}]),
        snapshot_payload(candles=[{"date": "2026-06-01", "closing_price": "1"}]),
        snapshot_payload(candles=[{"date": "2026-06-01", "closing_price": -1}]),
        snapshot_payload(candles=[{"date": "2026-06-01", "closing_price": 1}] * 31),
    ],
)
@pytest.mark.asyncio
async def test_invalid_fresh_cache_is_miss_not_empty_success(payload: str) -> None:
    redis = FakeRedis()
    redis.values[chart_key("KRW-BTC")] = payload
    source = FakeCandleSource([candle(1, 10)])

    result = await make_service(source, redis).get_chart("KRW-BTC")

    assert len(result.candles) == 1
    assert source.calls == [("KRW-BTC", None)]


@pytest.mark.asyncio
async def test_miss_transforms_sorts_and_writes_both_1d_keys() -> None:
    events: list[str] = []
    redis = FakeRedis()
    market_service = FakeMarketListService(events=events)
    source = FakeCandleSource(
        [candle(3, 100.125), candle(1, 0), candle(2, 99)],
        events=events,
    )

    result = await make_service(source, redis, market_service).get_chart("KRW-BTC")

    assert events == ["market", "upbit"]
    assert [item.date.isoformat() for item in result.candles] == [
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
    ]
    assert [item.closing_price for item in result.candles] == [
        Decimal("0"),
        Decimal("99"),
        Decimal("100.125"),
    ]
    assert source.calls == [("KRW-BTC", None)]
    assert redis.ttls == {
        "chart:KRW-BTC:1d": CHART_TTL_SECONDS,
        "stale:chart:KRW-BTC:1d": STALE_CHART_TTL_SECONDS,
    }
    assert all(":1m" not in key for key in redis.ttls)
    stored = json.loads(redis.values[chart_key("KRW-BTC")])
    assert stored["candles"][0]["closing_price"] == 0
    assert stored["candles"][2]["closing_price"] == 100.125


@pytest.mark.parametrize("count", [0, 1, 2, 29, 30])
@pytest.mark.asyncio
async def test_zero_through_thirty_candles_are_valid(count: int) -> None:
    candles = [candle((index % 28) + 1, index) for index in range(count)]

    result = await make_service(FakeCandleSource(candles), FakeRedis()).get_chart(
        "KRW-BTC"
    )

    assert len(result.candles) == count


@pytest.mark.asyncio
async def test_more_than_thirty_or_negative_price_is_unavailable_and_not_cached() -> (
    None
):
    for candles in (
        [candle(1, 1)] * 31,
        [candle(1, -1)],
    ):
        redis = FakeRedis()
        with pytest.raises(AppError) as captured:
            await make_service(FakeCandleSource(candles), redis).get_chart("KRW-BTC")

        assert captured.value.code is ErrorCode.UPBIT_UNAVAILABLE
        assert chart_key("KRW-BTC") not in redis.values
        assert stale_chart_key("KRW-BTC") not in redis.values


@pytest.mark.asyncio
async def test_418_uses_only_valid_stale_without_refreshing_it() -> None:
    redis = FakeRedis()
    redis.values[stale_chart_key("KRW-BTC")] = snapshot_payload(
        candles=[{"date": "2026-06-01", "closing_price": 7}]
    )
    blocked = AppError(
        code=ErrorCode.UPBIT_TEMPORARILY_BLOCKED,
        message="safe blocked",
    )
    source = FakeCandleSource(error=blocked)

    result = await make_service(source, redis).get_chart("KRW-BTC")

    assert result.candles[0].closing_price == Decimal("7")
    assert source.calls == [("KRW-BTC", None)]
    assert redis.commands == [
        ("get", chart_key("KRW-BTC")),
        ("get", stale_chart_key("KRW-BTC")),
    ]


@pytest.mark.parametrize(
    "stale_payload",
    [
        None,
        snapshot_payload(market_code="KRW-ETH"),
        snapshot_payload(period="1d"),
        snapshot_payload(candles=[{"date": "bad", "closing_price": 1}]),
    ],
)
@pytest.mark.asyncio
async def test_418_without_valid_stale_preserves_blocked(
    stale_payload: str | None,
) -> None:
    redis = FakeRedis()
    if stale_payload is not None:
        redis.values[stale_chart_key("KRW-BTC")] = stale_payload
    blocked = AppError(
        code=ErrorCode.UPBIT_TEMPORARILY_BLOCKED,
        message="safe blocked",
    )

    with pytest.raises(AppError) as captured:
        await make_service(FakeCandleSource(error=blocked), redis).get_chart("KRW-BTC")

    assert captured.value.code is ErrorCode.UPBIT_TEMPORARILY_BLOCKED


@pytest.mark.asyncio
async def test_redis_read_failure_uses_exactly_one_no_retry_direct_call() -> None:
    redis = FakeRedis()
    redis.failures["get"] = ConnectionError("private Redis host")
    source = FakeCandleSource([candle(1, 1)])

    result = await make_service(source, redis).get_chart("KRW-BTC")

    assert len(result.candles) == 1
    assert source.calls == [("KRW-BTC", 0)]
    assert all(command[0] not in {"set", "pipeline"} for command in redis.commands)


@pytest.mark.asyncio
async def test_redis_direct_and_cache_write_failures_preserve_upbit_contract() -> None:
    redis = FakeRedis()
    redis.failures["get"] = ConnectionError("private Redis host")
    rate_limited = AppError(
        code=ErrorCode.UPBIT_RATE_LIMITED,
        message="safe rate limit",
    )
    source = FakeCandleSource(error=rate_limited)

    with pytest.raises(AppError) as captured:
        await make_service(source, redis).get_chart("KRW-BTC")

    assert captured.value is rate_limited
    assert source.calls == [("KRW-BTC", 0)]

    write_failure_redis = FakeRedis()
    write_failure_redis.failures["pipeline_execute"] = ConnectionError("private")
    result = await make_service(
        FakeCandleSource([candle(1, 3)]),
        write_failure_redis,
    ).get_chart("KRW-BTC")
    assert result.candles[0].closing_price == Decimal("3")


@pytest.mark.asyncio
async def test_unexpected_upbit_4xx_is_safe_unavailable() -> None:
    error = UpbitClientResponseError(status_code=404, group=RateLimitGroup.CANDLE)

    with pytest.raises(AppError) as captured:
        await make_service(FakeCandleSource(error=error), FakeRedis()).get_chart(
            "KRW-BTC"
        )

    assert captured.value.code is ErrorCode.UPBIT_UNAVAILABLE
    assert captured.value.status_code == 502


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_or_cached() -> None:
    redis = FakeRedis()

    with pytest.raises(asyncio.CancelledError):
        await make_service(
            FakeCandleSource(error=asyncio.CancelledError()),
            redis,
        ).get_chart("KRW-BTC")

    assert chart_key("KRW-BTC") not in redis.values


def test_internal_snapshot_rejects_unsorted_cache_and_serializes_numbers() -> None:
    with pytest.raises(ValueError):
        ChartSnapshot(
            market_code="KRW-BTC",
            candles=(
                ChartCandle(date="2026-06-02", closing_price=2),  # type: ignore[arg-type]
                ChartCandle(date="2026-06-01", closing_price=1),  # type: ignore[arg-type]
            ),
        )
