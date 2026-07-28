import asyncio
import json
from collections.abc import Sequence

import pytest
from redis.exceptions import ConnectionError

from app.cache.keys import (
    MARKET_LIST_KEY,
    MARKET_LIST_LOCK_KEY,
    MARKET_LIST_LOCK_TTL_SECONDS,
    MARKET_LIST_TTL_SECONDS,
)
from app.cache.redis import RedisCache
from app.clients.upbit import RateLimitGroup, UpbitClientResponseError
from app.core.errors import AppError, ErrorCode
from app.models.market import Market, MarketStatus
from app.schemas.upbit import UpbitMarket
from app.services.market_list import MAX_SEARCH_RESULTS, MarketListService
from tests.test_redis_cache import FakeRedis


def _upbit_market(
    market: str,
    korean_name: str,
    english_name: str,
    warning: str = "NONE",
) -> UpbitMarket:
    return UpbitMarket(
        market=market,
        korean_name=korean_name,
        english_name=english_name,
        market_warning=warning,
    )


def _market(
    market_code: str,
    korean_name: str,
    english_name: str,
    status: MarketStatus = MarketStatus.ACTIVE,
) -> Market:
    return Market(
        market_code=market_code,
        korean_name=korean_name,
        english_name=english_name,
        status=status,
    )


def _cache_payload(markets: Sequence[Market]) -> str:
    return json.dumps([market.to_cache_value() for market in markets])


class FakeMarketSource:
    def __init__(
        self,
        markets: Sequence[UpbitMarket] = (),
        *,
        error: BaseException | None = None,
    ) -> None:
        self.markets = list(markets)
        self.error = error
        self.calls = 0

    async def get_markets(self) -> list[UpbitMarket]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.markets)


class MarketSetFailingRedis(FakeRedis):
    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool = False,
    ) -> bool | None:
        if name == MARKET_LIST_KEY:
            self.commands.append(("set", name, value, ex, nx))
            raise ConnectionError("private Redis connection")
        return await super().set(name, value, ex=ex, nx=nx)


class CacheAfterLockRedis(FakeRedis):
    def __init__(self, payload: str) -> None:
        super().__init__()
        self.payload = payload

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int,
        nx: bool = False,
    ) -> bool | None:
        result = await super().set(name, value, ex=ex, nx=nx)
        if name == MARKET_LIST_LOCK_KEY and nx and result:
            self.values[MARKET_LIST_KEY] = self.payload
        return result


@pytest.mark.asyncio
async def test_valid_cache_hit_updates_memory_without_lock_or_upbit() -> None:
    cached = (_market("KRW-BTC", "비트코인", "Bitcoin"),)
    fake = FakeRedis()
    fake.values[MARKET_LIST_KEY] = _cache_payload(cached)
    source = FakeMarketSource()
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
    )

    result = await service.get_markets()

    assert result == cached
    assert service.memory_snapshot == cached
    assert source.calls == 0
    assert fake.commands == [("get", MARKET_LIST_KEY)]


@pytest.mark.asyncio
async def test_cache_miss_owner_filters_krw_maps_status_and_stores_ttl() -> None:
    fake = FakeRedis()
    source = FakeMarketSource(
        [
            _upbit_market("KRW-BTC", "비트코인", "Bitcoin"),
            _upbit_market("KRW-XRP", "리플", "XRP", "CAUTION"),
            _upbit_market("BTC-ETH", "이더리움", "Ethereum"),
            _upbit_market("USDT-SOL", "솔라나", "Solana"),
        ]
    )
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
    )

    result = await service.get_markets()

    assert [(market.market_code, market.status) for market in result] == [
        ("KRW-BTC", MarketStatus.ACTIVE),
        ("KRW-XRP", MarketStatus.CAUTION),
    ]
    assert source.calls == 1
    assert service.memory_snapshot == result
    assert fake.ttls[MARKET_LIST_KEY] == MARKET_LIST_TTL_SECONDS == 86_400
    lock_sets = [
        command
        for command in fake.commands
        if command[0] == "set" and command[1] == MARKET_LIST_LOCK_KEY
    ]
    assert len(lock_sets) == 1
    assert lock_sets[0][3:] == (MARKET_LIST_LOCK_TTL_SECONDS, True)
    stored = json.loads(fake.values[MARKET_LIST_KEY])
    assert stored == [market.to_cache_value() for market in result]
    assert "BTC-ETH" not in fake.values[MARKET_LIST_KEY]
    assert "USDT-SOL" not in fake.values[MARKET_LIST_KEY]


@pytest.mark.asyncio
async def test_owner_rechecks_cache_after_lock_before_calling_upbit() -> None:
    cached = (_market("KRW-ETH", "이더리움", "Ethereum"),)
    fake = CacheAfterLockRedis(_cache_payload(cached))
    source = FakeMarketSource()
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
    )

    result = await service.get_markets()

    assert result == cached
    assert source.calls == 0
    assert [command[0] for command in fake.commands] == ["get", "set", "get", "eval"]


@pytest.mark.asyncio
async def test_cache_store_failure_keeps_validated_memory_result() -> None:
    fake = MarketSetFailingRedis()
    source = FakeMarketSource([_upbit_market("KRW-BTC", "비트코인", "Bitcoin")])
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
    )

    result = await service.get_markets()

    assert result == service.memory_snapshot
    assert result[0].market_code == "KRW-BTC"
    assert source.calls == 1
    assert fake.values.get(MARKET_LIST_KEY) is None
    assert any(command[0] == "eval" for command in fake.commands)


@pytest.mark.asyncio
async def test_lock_loser_rechecks_cache_and_never_calls_upbit() -> None:
    cached = (_market("KRW-SOL", "솔라나", "Solana"),)
    fake = FakeRedis()
    fake.values[MARKET_LIST_LOCK_KEY] = "other-owner"
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            fake.values[MARKET_LIST_KEY] = _cache_payload(cached)

    source = FakeMarketSource()
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
        sleeper=sleeper,
    )

    result = await service.get_markets()

    assert result == cached
    assert sleeps == [0.1, 0.1, 0.1]
    assert source.calls == 0
    assert fake.values[MARKET_LIST_LOCK_KEY] == "other-owner"


@pytest.mark.asyncio
async def test_lock_loser_stops_after_five_checks_with_expected_error() -> None:
    fake = FakeRedis()
    fake.values[MARKET_LIST_LOCK_KEY] = "other-owner"
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    service = MarketListService(
        upbit_client=FakeMarketSource(),
        redis_cache=RedisCache(fake),
        sleeper=sleeper,
    )

    with pytest.raises(AppError) as exc_info:
        await service.get_markets()

    assert sleeps == [0.1] * 5
    assert exc_info.value.code is ErrorCode.CACHE_REFRESH_IN_PROGRESS
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_redis_failure_uses_existing_memory_without_upbit() -> None:
    cached = (_market("KRW-BTC", "비트코인", "Bitcoin"),)
    fake = FakeRedis()
    fake.values[MARKET_LIST_KEY] = _cache_payload(cached)
    source = FakeMarketSource()
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
    )
    await service.get_markets()
    fake.failures["get"] = ConnectionError("private Redis host")

    result = await service.get_markets()

    assert result == cached
    assert source.calls == 0


@pytest.mark.asyncio
async def test_redis_failure_without_memory_fetches_upbit_once_and_fills_memory() -> (
    None
):
    fake = FakeRedis()
    fake.failures["get"] = ConnectionError("private Redis host")
    source = FakeMarketSource([_upbit_market("KRW-ETH", "이더리움", "Ethereum")])
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
    )

    result = await service.get_markets()

    assert result == service.memory_snapshot
    assert result[0].market_code == "KRW-ETH"
    assert source.calls == 1
    assert all(command[0] != "set" for command in fake.commands)


@pytest.mark.asyncio
async def test_redis_lock_failure_without_memory_uses_direct_upbit_fallback() -> None:
    fake = FakeRedis()
    fake.failures["set"] = ConnectionError("private Redis lock failure")
    source = FakeMarketSource([_upbit_market("KRW-SOL", "솔라나", "Solana")])
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
    )

    result = await service.get_markets()

    assert source.calls == 1
    assert result == service.memory_snapshot
    assert result[0].market_code == "KRW-SOL"


@pytest.mark.parametrize("payload", ["not-json", json.dumps({"marketCode": "KRW-BTC"})])
@pytest.mark.asyncio
async def test_invalid_cache_payload_is_not_used_as_hit(payload: str) -> None:
    fake = FakeRedis()
    fake.values[MARKET_LIST_KEY] = payload
    source = FakeMarketSource([_upbit_market("KRW-XRP", "리플", "XRP", "CAUTION")])
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
    )

    result = await service.get_markets()

    assert source.calls == 1
    assert result[0].market_code == "KRW-XRP"
    assert result[0].status is MarketStatus.CAUTION


@pytest.mark.asyncio
async def test_invalid_cache_does_not_replace_previous_memory_on_upbit_failure() -> (
    None
):
    original = (_market("KRW-BTC", "비트코인", "Bitcoin"),)
    fake = FakeRedis()
    fake.values[MARKET_LIST_KEY] = _cache_payload(original)
    source = FakeMarketSource()
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
    )
    await service.get_markets()
    fake.values[MARKET_LIST_KEY] = json.dumps([{"status": "UNKNOWN"}])
    source.error = AppError(
        code=ErrorCode.UPBIT_UNAVAILABLE,
        message="safe Upbit failure",
    )

    with pytest.raises(AppError):
        await service.get_markets()

    assert service.memory_snapshot == original


@pytest.mark.parametrize("warning", ["", "UNKNOWN", "ACTIVE"])
@pytest.mark.asyncio
async def test_unknown_upbit_warning_is_not_treated_as_active(warning: str) -> None:
    fake = FakeRedis()
    source = FakeMarketSource(
        [_upbit_market("KRW-BTC", "비트코인", "Bitcoin", warning)]
    )
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
    )

    with pytest.raises(AppError) as exc_info:
        await service.get_markets()

    assert exc_info.value.code is ErrorCode.UPBIT_UNAVAILABLE
    assert MARKET_LIST_LOCK_KEY not in fake.values
    assert any(command[0] == "eval" for command in fake.commands)


@pytest.mark.asyncio
async def test_existing_upbit_app_error_and_cancellation_propagate_with_unlock() -> (
    None
):
    for error in (
        AppError(code=ErrorCode.UPBIT_RATE_LIMITED, message="safe rate limit"),
        asyncio.CancelledError(),
    ):
        fake = FakeRedis()
        source = FakeMarketSource(error=error)
        service = MarketListService(
            upbit_client=source,
            redis_cache=RedisCache(fake),
        )

        with pytest.raises(type(error)):
            await service.get_markets()

        assert MARKET_LIST_LOCK_KEY not in fake.values


@pytest.mark.asyncio
async def test_non_retryable_upbit_response_error_maps_to_502() -> None:
    source = FakeMarketSource(
        error=UpbitClientResponseError(status_code=404, group=RateLimitGroup.MARKET),
    )
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(FakeRedis()),
    )

    with pytest.raises(AppError) as exc_info:
        await service.get_markets()

    assert exc_info.value.code is ErrorCode.UPBIT_UNAVAILABLE
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_search_uses_best_tier_casefold_and_deterministic_ties() -> None:
    markets = (
        _market("KRW-AAA", "가코인", "Wrapped Bitcoin"),
        _market("KRW-BBB", "나코인", "Bitcoin Cash"),
        _market("KRW-CCC", "다코인", "Bitcoin"),
        _market("KRW-DDD", "가코인", "Coin Bitcoin"),
        _market("KRW-EEE", "가코인", "Coin Bitcoin"),
    )
    fake = FakeRedis()
    fake.values[MARKET_LIST_KEY] = _cache_payload(markets)
    service = MarketListService(
        upbit_client=FakeMarketSource(),
        redis_cache=RedisCache(fake),
    )

    result = await service.search("  bItCoIn  ")

    assert [market.market_code for market in result] == [
        "KRW-CCC",
        "KRW-BBB",
        "KRW-DDD",
        "KRW-EEE",
        "KRW-AAA",
    ]


@pytest.mark.asyncio
async def test_search_matches_korean_english_and_market_code() -> None:
    markets = (
        _market("KRW-BTC", "비트코인", "Bitcoin"),
        _market("KRW-ETH", "이더리움", "Ethereum"),
    )
    fake = FakeRedis()
    fake.values[MARKET_LIST_KEY] = _cache_payload(markets)
    service = MarketListService(
        upbit_client=FakeMarketSource(),
        redis_cache=RedisCache(fake),
    )

    assert (await service.search("비트"))[0].market_code == "KRW-BTC"
    assert (await service.search("THER"))[0].market_code == "KRW-ETH"
    assert (await service.search("krw-b"))[0].market_code == "KRW-BTC"
    assert await service.search("없음") == ()


@pytest.mark.asyncio
async def test_search_limits_results_to_twenty() -> None:
    markets = tuple(
        _market(f"KRW-C{index:02d}", f"코인{index:02d}", f"Coin {index:02d}")
        for index in range(25)
    )
    fake = FakeRedis()
    fake.values[MARKET_LIST_KEY] = _cache_payload(markets)
    service = MarketListService(
        upbit_client=FakeMarketSource(),
        redis_cache=RedisCache(fake),
    )

    result = await service.search("coin")

    assert len(result) == MAX_SEARCH_RESULTS == 20
    assert result[0].korean_name == "코인00"
    assert result[-1].korean_name == "코인19"


@pytest.mark.asyncio
async def test_search_rejects_empty_or_whitespace_query() -> None:
    service = MarketListService(
        upbit_client=FakeMarketSource(),
        redis_cache=RedisCache(FakeRedis()),
    )

    for query in ("", "   "):
        with pytest.raises(AppError) as exc_info:
            await service.search(query)
        assert exc_info.value.code is ErrorCode.INVALID_REQUEST
        assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_concurrent_miss_has_one_upbit_refresh_owner() -> None:
    fake = FakeRedis()
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingSource(FakeMarketSource):
        async def get_markets(self) -> list[UpbitMarket]:
            self.calls += 1
            started.set()
            await release.wait()
            return [_upbit_market("KRW-BTC", "비트코인", "Bitcoin")]

    async def sleeper(seconds: float) -> None:
        await release.wait()

    source = BlockingSource()
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(fake),
        sleeper=sleeper,
    )

    owner = asyncio.create_task(service.get_markets())
    await started.wait()
    loser = asyncio.create_task(service.get_markets())
    await asyncio.sleep(0)
    release.set()
    owner_result, loser_result = await asyncio.gather(owner, loser)

    assert owner_result == loser_result
    assert source.calls == 1
