import json

import pytest
from redis.exceptions import ConnectionError

from app.cache.keys import MARKET_LIST_KEY
from app.cache.redis import RedisCache
from app.models.market import Market, MarketStatus
from app.services.market_list import MarketListService
from tests.test_market_list_service import FakeMarketSource
from tests.test_redis_cache import FakeRedis


MARKETS = (
    Market(
        market_code="KRW-BTC",
        korean_name="비트코인",
        english_name="Bitcoin",
        status=MarketStatus.ACTIVE,
    ),
    Market(
        market_code="KRW-BCH",
        korean_name="비트코인캐시",
        english_name="Bitcoin Cash",
        status=MarketStatus.CAUTION,
    ),
)


def cached_service() -> tuple[MarketListService, FakeRedis, FakeMarketSource]:
    redis = FakeRedis()
    redis.values[MARKET_LIST_KEY] = json.dumps(
        [market.to_cache_value() for market in MARKETS]
    )
    source = FakeMarketSource()
    service = MarketListService(
        upbit_client=source,
        redis_cache=RedisCache(redis),
    )
    return service, redis, source


@pytest.mark.asyncio
async def test_exact_lookup_uses_valid_cache_without_upbit() -> None:
    service, redis, source = cached_service()

    result = await service.get_market_by_code("KRW-BTC")

    assert result == MARKETS[0]
    assert source.calls == 0
    assert redis.commands == [("get", MARKET_LIST_KEY)]


@pytest.mark.asyncio
async def test_exact_lookup_does_not_use_partial_or_casefold_match() -> None:
    service, _, _ = cached_service()

    assert await service.get_market_by_code("KRW-BT") is None
    assert await service.get_market_by_code("krw-btc") is None


@pytest.mark.asyncio
async def test_exact_lookup_reuses_memory_when_redis_fails() -> None:
    service, redis, source = cached_service()
    assert await service.get_market_by_code("KRW-BTC") == MARKETS[0]
    redis.failures["get"] = ConnectionError("private Redis host")

    result = await service.get_market_by_code("KRW-BCH")

    assert result == MARKETS[1]
    assert source.calls == 0
