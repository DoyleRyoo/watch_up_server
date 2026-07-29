"""Bulk current-price cache, ticker refresh, and stale fallback service."""

import logging
from collections.abc import Callable, Sequence
from typing import Protocol

from pydantic import ValidationError

from app.cache.keys import (
    PRICE_TTL_SECONDS,
    STALE_PRICE_TTL_SECONDS,
    TICKER_REFRESH_LOCK_KEY,
    TICKER_REFRESH_LOCK_TTL_SECONDS,
    price_key,
    stale_price_key,
)
from app.cache.lock import RedisLockManager, Sleeper, wait_for_cache_refresh
from app.cache.redis import CacheWrite, RedisCache, RedisUnavailableError
from app.clients.upbit import (
    UPBIT_ERROR_MESSAGE,
    UpbitClient,
    UpbitClientResponseError,
)
from app.core.errors import AppError, ErrorCode
from app.models.price import PriceQuote, ResolvedPrice
from app.schemas.upbit import UpbitTicker


logger = logging.getLogger("uvicorn.error")


class TickerSource(Protocol):
    async def get_tickers(
        self,
        market_codes: Sequence[str],
        *,
        max_retries: int | None = None,
    ) -> list[UpbitTicker]: ...


PriceServiceFactory = Callable[[UpbitClient, RedisCache], "PriceService"]


class PriceService:
    """Resolves a stable set of market prices with one Redis/Upbit batch flow."""

    def __init__(
        self,
        *,
        upbit_client: TickerSource,
        redis_cache: RedisCache,
        lock_manager: RedisLockManager | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._upbit_client = upbit_client
        self._redis_cache = redis_cache
        self._lock_manager = lock_manager or RedisLockManager(redis_cache)
        self._sleeper = sleeper

    async def get_prices(
        self,
        market_codes: Sequence[str],
    ) -> dict[str, ResolvedPrice]:
        requested = tuple(dict.fromkeys(market_codes))
        if not requested:
            return {}

        try:
            resolved = await self._read_prices(requested, stale=False)
        except RedisUnavailableError:
            return await self._direct_without_redis(requested)

        missing = _missing_codes(requested, resolved)
        if not missing:
            return resolved

        try:
            async with self._lock_manager.lock(
                key=TICKER_REFRESH_LOCK_KEY,
                ttl_seconds=TICKER_REFRESH_LOCK_TTL_SECONDS,
            ) as lease:
                if lease is None:
                    waited = await self._wait_for_owner(missing)
                    return resolved | waited

                try:
                    double_checked = await self._read_prices(missing, stale=False)
                except RedisUnavailableError:
                    return resolved | await self._direct_without_redis(missing)
                resolved.update(double_checked)
                remaining = _missing_codes(missing, double_checked)
                if not remaining:
                    return resolved

                fetched = await self._fetch_with_stale_fallback(remaining)
                resolved.update(fetched)
                return resolved
        except RedisUnavailableError:
            return resolved | await self._direct_without_redis(missing)

    async def _wait_for_owner(
        self,
        market_codes: tuple[str, ...],
    ) -> dict[str, ResolvedPrice]:
        async def read_complete() -> dict[str, ResolvedPrice] | None:
            cached = await self._read_prices(market_codes, stale=False)
            return cached if len(cached) == len(market_codes) else None

        if self._sleeper is None:
            return await wait_for_cache_refresh(read_complete)
        return await wait_for_cache_refresh(read_complete, sleeper=self._sleeper)

    async def _fetch_with_stale_fallback(
        self,
        market_codes: tuple[str, ...],
    ) -> dict[str, ResolvedPrice]:
        try:
            quotes = await self._fetch_quotes(market_codes, max_retries=None)
        except AppError as exc:
            if exc.code is not ErrorCode.UPBIT_TEMPORARILY_BLOCKED:
                raise
            try:
                stale = await self._read_prices(market_codes, stale=True)
            except RedisUnavailableError:
                raise exc
            if len(stale) != len(market_codes):
                raise exc
            return stale

        try:
            await self._write_prices(tuple(result.quote for result in quotes.values()))
        except RedisUnavailableError:
            logger.warning("Redis unavailable while storing validated ticker prices")
        return quotes

    async def _direct_without_redis(
        self,
        market_codes: tuple[str, ...],
    ) -> dict[str, ResolvedPrice]:
        logger.warning("Fetching ticker prices once without Redis coordination")
        return await self._fetch_quotes(market_codes, max_retries=0)

    async def _fetch_quotes(
        self,
        market_codes: tuple[str, ...],
        *,
        max_retries: int | None,
    ) -> dict[str, ResolvedPrice]:
        try:
            tickers = await self._upbit_client.get_tickers(
                market_codes,
                max_retries=max_retries,
            )
        except UpbitClientResponseError:
            raise AppError(
                code=ErrorCode.UPBIT_UNAVAILABLE,
                message=UPBIT_ERROR_MESSAGE,
            ) from None

        requested = set(market_codes)
        resolved: dict[str, ResolvedPrice] = {}
        try:
            for ticker in tickers:
                if ticker.market not in requested or ticker.market in resolved:
                    continue
                quote = PriceQuote.from_upbit(ticker)
                resolved[ticker.market] = ResolvedPrice(quote=quote, is_stale=False)
        except ValidationError:
            raise AppError(
                code=ErrorCode.UPBIT_UNAVAILABLE,
                message=UPBIT_ERROR_MESSAGE,
            ) from None
        return resolved

    async def _read_prices(
        self,
        market_codes: tuple[str, ...],
        *,
        stale: bool,
    ) -> dict[str, ResolvedPrice]:
        key_builder = stale_price_key if stale else price_key
        payloads = await self._redis_cache.get_many_json(
            [key_builder(market_code) for market_code in market_codes]
        )
        resolved: dict[str, ResolvedPrice] = {}
        for expected_code, payload in zip(market_codes, payloads, strict=True):
            if payload is None:
                continue
            try:
                quote = PriceQuote.model_validate(payload)
            except ValidationError:
                continue
            if quote.market_code != expected_code:
                continue
            resolved[expected_code] = ResolvedPrice(quote=quote, is_stale=stale)
        return resolved

    async def _write_prices(self, quotes: tuple[PriceQuote, ...]) -> None:
        writes: list[CacheWrite] = []
        for quote in quotes:
            value = quote.to_cache_value()
            writes.extend(
                [
                    CacheWrite(
                        key=price_key(quote.market_code),
                        value=value,
                        ttl_seconds=PRICE_TTL_SECONDS,
                    ),
                    CacheWrite(
                        key=stale_price_key(quote.market_code),
                        value=value,
                        ttl_seconds=STALE_PRICE_TTL_SECONDS,
                    ),
                ]
            )
        await self._redis_cache.set_many_json(writes)


def _missing_codes(
    requested: tuple[str, ...],
    resolved: dict[str, ResolvedPrice],
) -> tuple[str, ...]:
    return tuple(code for code in requested if code not in resolved)


def create_price_service(
    upbit_client: UpbitClient,
    redis_cache: RedisCache,
) -> PriceService:
    return PriceService(upbit_client=upbit_client, redis_cache=redis_cache)


__all__ = [
    "PriceService",
    "PriceServiceFactory",
    "TickerSource",
    "create_price_service",
]
