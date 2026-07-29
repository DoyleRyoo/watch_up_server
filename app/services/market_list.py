"""Cached KRW market-list loading and local coin search."""

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Final, Protocol

from pydantic import TypeAdapter, ValidationError

from app.cache.keys import (
    MARKET_LIST_KEY,
    MARKET_LIST_LOCK_KEY,
    MARKET_LIST_LOCK_TTL_SECONDS,
    MARKET_LIST_TTL_SECONDS,
)
from app.cache.lock import RedisLockManager, Sleeper, wait_for_cache_refresh
from app.cache.redis import RedisCache, RedisUnavailableError
from app.clients.upbit import UPBIT_ERROR_MESSAGE, UpbitClient, UpbitClientResponseError
from app.core.errors import AppError, ErrorCode, INVALID_REQUEST_MESSAGE
from app.models.market import Market, MarketStatus
from app.schemas.upbit import UpbitMarket


logger = logging.getLogger("uvicorn.error")
MAX_SEARCH_RESULTS: Final = 20
MARKET_STATUS_BY_WARNING: Final[Mapping[str, MarketStatus]] = {
    "NONE": MarketStatus.ACTIVE,
    "CAUTION": MarketStatus.CAUTION,
}
MARKET_LIST_ADAPTER = TypeAdapter(list[Market])


class MarketSource(Protocol):
    async def get_markets(self) -> Sequence[UpbitMarket]: ...


MarketListServiceFactory = Callable[[UpbitClient, RedisCache], "MarketListService"]


class MarketListService:
    """Owns one immutable process-local snapshot for a single app worker."""

    def __init__(
        self,
        *,
        upbit_client: MarketSource,
        redis_cache: RedisCache,
        lock_manager: RedisLockManager | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._upbit_client = upbit_client
        self._redis_cache = redis_cache
        self._lock_manager = lock_manager or RedisLockManager(redis_cache)
        self._sleeper = sleeper
        self._memory_snapshot: tuple[Market, ...] | None = None

    @property
    def memory_snapshot(self) -> tuple[Market, ...] | None:
        return self._memory_snapshot

    async def get_markets(self) -> tuple[Market, ...]:
        try:
            cached = await self._read_cached_markets()
        except RedisUnavailableError:
            return await self._markets_without_redis()

        if cached is not None:
            self._replace_memory_snapshot(cached)
            return cached
        return await self.refresh_markets()

    async def refresh_markets(self) -> tuple[Market, ...]:
        try:
            async with self._lock_manager.lock(
                key=MARKET_LIST_LOCK_KEY,
                ttl_seconds=MARKET_LIST_LOCK_TTL_SECONDS,
            ) as lease:
                if lease is None:
                    return await self._wait_for_refresh_owner()

                try:
                    cached = await self._read_cached_markets()
                except RedisUnavailableError:
                    return await self._markets_without_redis()
                if cached is not None:
                    self._replace_memory_snapshot(cached)
                    return cached

                markets = await self._fetch_krw_markets()
                self._replace_memory_snapshot(markets)
                try:
                    await self._redis_cache.set_json(
                        MARKET_LIST_KEY,
                        [market.to_cache_value() for market in markets],
                        ttl_seconds=MARKET_LIST_TTL_SECONDS,
                    )
                except RedisUnavailableError:
                    logger.warning(
                        "Redis unavailable while storing validated market snapshot"
                    )
                return markets
        except RedisUnavailableError:
            return await self._markets_without_redis()

    async def search(self, query: str) -> tuple[Market, ...]:
        normalized_query = query.strip()
        if not normalized_query:
            raise AppError(
                code=ErrorCode.INVALID_REQUEST,
                message=INVALID_REQUEST_MESSAGE,
            )

        folded_query = normalized_query.casefold()
        ranked: list[tuple[int, Market]] = []
        for market in await self.get_markets():
            rank = _best_match_rank(market, folded_query)
            if rank is not None:
                ranked.append((rank, market))

        ranked.sort(
            key=lambda item: (
                item[0],
                item[1].korean_name.casefold(),
                item[1].english_name.casefold(),
                item[1].market_code.casefold(),
            )
        )
        return tuple(market for _, market in ranked[:MAX_SEARCH_RESULTS])

    async def get_market_by_code(self, market_code: str) -> Market | None:
        """Return only an exact market-code match from the validated snapshot."""
        return next(
            (
                market
                for market in await self.get_markets()
                if market.market_code == market_code
            ),
            None,
        )

    async def _wait_for_refresh_owner(self) -> tuple[Market, ...]:
        try:
            if self._sleeper is None:
                markets = await wait_for_cache_refresh(self._read_cached_markets)
            else:
                markets = await wait_for_cache_refresh(
                    self._read_cached_markets,
                    sleeper=self._sleeper,
                )
        except RedisUnavailableError:
            return await self._markets_without_redis()
        self._replace_memory_snapshot(markets)
        return markets

    async def _read_cached_markets(self) -> tuple[Market, ...] | None:
        payload = await self._redis_cache.get_json(MARKET_LIST_KEY)
        if payload is None:
            return None
        try:
            return tuple(MARKET_LIST_ADAPTER.validate_python(payload))
        except ValidationError:
            logger.warning("Ignoring invalid market list cache payload")
            return None

    async def _markets_without_redis(self) -> tuple[Market, ...]:
        snapshot = self._memory_snapshot
        if snapshot is not None:
            logger.warning(
                "Using process-local market snapshot while Redis is unavailable"
            )
            return snapshot

        logger.warning("Refreshing market snapshot without Redis coordination")
        markets = await self._fetch_krw_markets()
        self._replace_memory_snapshot(markets)
        return markets

    async def _fetch_krw_markets(self) -> tuple[Market, ...]:
        try:
            upbit_markets = await self._upbit_client.get_markets()
        except UpbitClientResponseError:
            raise AppError(
                code=ErrorCode.UPBIT_UNAVAILABLE,
                message=UPBIT_ERROR_MESSAGE,
            ) from None

        markets: list[Market] = []
        try:
            for upbit_market in upbit_markets:
                if not upbit_market.market.startswith("KRW-"):
                    continue
                status = MARKET_STATUS_BY_WARNING[upbit_market.market_warning]
                markets.append(
                    Market(
                        market_code=upbit_market.market,
                        korean_name=upbit_market.korean_name,
                        english_name=upbit_market.english_name,
                        status=status,
                    )
                )
        except (KeyError, ValidationError):
            raise AppError(
                code=ErrorCode.UPBIT_UNAVAILABLE,
                message=UPBIT_ERROR_MESSAGE,
            ) from None
        return tuple(markets)

    def _replace_memory_snapshot(self, markets: tuple[Market, ...]) -> None:
        self._memory_snapshot = tuple(markets)


def _best_match_rank(market: Market, folded_query: str) -> int | None:
    fields = (
        market.korean_name.casefold(),
        market.english_name.casefold(),
        market.market_code.casefold(),
    )
    if any(field == folded_query for field in fields):
        return 0
    if any(field.startswith(folded_query) for field in fields):
        return 1
    if any(folded_query in field for field in fields):
        return 2
    return None


def create_market_list_service(
    upbit_client: UpbitClient,
    redis_cache: RedisCache,
) -> MarketListService:
    return MarketListService(upbit_client=upbit_client, redis_cache=redis_cache)


__all__ = [
    "MAX_SEARCH_RESULTS",
    "MarketListService",
    "MarketListServiceFactory",
    "create_market_list_service",
]
