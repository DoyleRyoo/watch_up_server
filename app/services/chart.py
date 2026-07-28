"""Daily chart validation, cache, Upbit fetch, and stale fallback."""

import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Final, Protocol

from pydantic import ValidationError

from app.cache.keys import (
    CHART_TTL_SECONDS,
    STALE_CHART_TTL_SECONDS,
    chart_key,
    stale_chart_key,
)
from app.cache.redis import RedisCache, RedisUnavailableError
from app.clients.upbit import (
    UPBIT_ERROR_MESSAGE,
    UpbitClient,
    UpbitClientResponseError,
)
from app.core.errors import AppError, ErrorCode
from app.models.chart import CHART_RESPONSE_PERIOD, ChartCandle, ChartSnapshot
from app.models.watchlist import MARKET_CODE_PATTERN as MARKET_CODE_PATTERN_TEXT
from app.schemas.upbit import UpbitDayCandle
from app.services.market_list import MarketListService


logger = logging.getLogger("uvicorn.error")
MARKET_CODE_MAX_LENGTH: Final = 20
MARKET_CODE_PATTERN: Final = re.compile(MARKET_CODE_PATTERN_TEXT)
INVALID_MARKET_CODE_MESSAGE: Final = "유효하지 않은 마켓 코드입니다."


class CandleSource(Protocol):
    async def get_day_candles(
        self,
        market_code: str,
        *,
        max_retries: int | None = None,
    ) -> list[UpbitDayCandle]: ...


ChartServiceFactory = Callable[
    [UpbitClient, RedisCache, MarketListService],
    "ChartService",
]


class ChartService:
    """Resolves one validated chart without watchlist or database access."""

    def __init__(
        self,
        *,
        upbit_client: CandleSource,
        redis_cache: RedisCache,
        market_list_service: MarketListService,
    ) -> None:
        self._upbit_client = upbit_client
        self._redis_cache = redis_cache
        self._market_list_service = market_list_service

    async def get_chart(self, market_code: str) -> ChartSnapshot:
        _validate_market_code(market_code)

        market = await self._market_list_service.get_market_by_code(market_code)
        if market is None:
            raise _invalid_market_code()

        try:
            cached = await self._read_chart(market_code, stale=False)
        except RedisUnavailableError:
            return await self._direct_without_redis(market_code)
        if cached is not None:
            return cached

        return await self._fetch_with_stale_fallback(market_code)

    async def _fetch_with_stale_fallback(
        self,
        market_code: str,
    ) -> ChartSnapshot:
        try:
            snapshot = await self._fetch_chart(market_code, max_retries=None)
        except AppError as exc:
            if exc.code is not ErrorCode.UPBIT_TEMPORARILY_BLOCKED:
                raise
            try:
                stale = await self._read_chart(market_code, stale=True)
            except RedisUnavailableError:
                raise exc
            if stale is None:
                raise exc
            return stale

        try:
            await self._redis_cache.set_fresh_and_stale(
                fresh_key=chart_key(market_code),
                fresh_ttl_seconds=CHART_TTL_SECONDS,
                stale_key=stale_chart_key(market_code),
                stale_ttl_seconds=STALE_CHART_TTL_SECONDS,
                value=snapshot.to_cache_value(),
            )
        except RedisUnavailableError:
            logger.warning("Redis unavailable while storing validated chart snapshot")
        return snapshot

    async def _direct_without_redis(self, market_code: str) -> ChartSnapshot:
        logger.warning("Fetching daily chart once without Redis coordination")
        return await self._fetch_chart(market_code, max_retries=0)

    async def _fetch_chart(
        self,
        market_code: str,
        *,
        max_retries: int | None,
    ) -> ChartSnapshot:
        try:
            upbit_candles = await self._upbit_client.get_day_candles(
                market_code,
                max_retries=max_retries,
            )
        except UpbitClientResponseError:
            raise AppError(
                code=ErrorCode.UPBIT_UNAVAILABLE,
                message=UPBIT_ERROR_MESSAGE,
            ) from None

        if len(upbit_candles) > 30:
            raise _upbit_unavailable()

        try:
            candles = tuple(
                sorted(
                    (_to_chart_candle(candle) for candle in upbit_candles),
                    key=lambda candle: candle.date,
                )
            )
            return ChartSnapshot(
                market_code=market_code,
                period=CHART_RESPONSE_PERIOD,
                candles=candles,
            )
        except (ValueError, ValidationError):
            raise _upbit_unavailable() from None

    async def _read_chart(
        self,
        market_code: str,
        *,
        stale: bool,
    ) -> ChartSnapshot | None:
        key = stale_chart_key(market_code) if stale else chart_key(market_code)
        payload = await self._redis_cache.get_json(key)
        if payload is None:
            return None
        try:
            snapshot = ChartSnapshot.model_validate(payload)
        except ValidationError:
            logger.warning("Ignoring invalid chart cache payload")
            return None
        if snapshot.market_code != market_code:
            return None
        return snapshot


def _to_chart_candle(candle: UpbitDayCandle) -> ChartCandle:
    return ChartCandle(
        date=datetime.fromisoformat(candle.candle_date_time_kst).date(),
        closing_price=candle.trade_price,
    )


def _validate_market_code(market_code: str) -> None:
    if (
        len(market_code) > MARKET_CODE_MAX_LENGTH
        or MARKET_CODE_PATTERN.fullmatch(market_code) is None
    ):
        raise _invalid_market_code()


def _invalid_market_code() -> AppError:
    return AppError(
        code=ErrorCode.INVALID_MARKET_CODE,
        message=INVALID_MARKET_CODE_MESSAGE,
    )


def _upbit_unavailable() -> AppError:
    return AppError(
        code=ErrorCode.UPBIT_UNAVAILABLE,
        message=UPBIT_ERROR_MESSAGE,
    )


def create_chart_service(
    upbit_client: UpbitClient,
    redis_cache: RedisCache,
    market_list_service: MarketListService,
) -> ChartService:
    return ChartService(
        upbit_client=upbit_client,
        redis_cache=redis_cache,
        market_list_service=market_list_service,
    )


__all__ = [
    "CandleSource",
    "ChartService",
    "ChartServiceFactory",
    "create_chart_service",
]
