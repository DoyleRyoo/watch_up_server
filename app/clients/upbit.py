import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Final, TypeVar

import httpx
from pydantic import TypeAdapter

from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.schemas.upbit import UpbitDayCandle, UpbitMarket, UpbitTicker


logger = logging.getLogger("uvicorn.error")

TOTAL_REQUEST_BUDGET_SECONDS: Final[float] = 8
MAX_ATTEMPT_TIMEOUT_SECONDS: Final[float] = 5
MAX_RETRIES: Final[int] = 2
DEFAULT_RATE_LIMIT_DELAY_SECONDS: Final[float] = 1
RETRY_BACKOFF_SECONDS: Final[tuple[float, ...]] = (0.5, 1.0)
UPBIT_ERROR_MESSAGE: Final[str] = "시세 정보를 일시적으로 조회할 수 없습니다."

ResponseT = TypeVar("ResponseT")
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class RateLimitGroup(StrEnum):
    MARKET = "market"
    TICKER = "ticker"
    CANDLE = "candle"


@dataclass(frozen=True, slots=True)
class RemainingRequest:
    group: str | None
    sec: int | None


@dataclass(slots=True)
class _RateLimitState:
    lock: asyncio.Lock
    blocked_until: float = 0


class _RequestBudgetExceeded(Exception):
    pass


class UpbitClientResponseError(Exception):
    """Non-retryable Upbit 4xx awaiting service-level interpretation."""

    def __init__(self, *, status_code: int, group: RateLimitGroup) -> None:
        super().__init__(f"Upbit request rejected with HTTP {status_code}")
        self.status_code = status_code
        self.group = group


def parse_remaining_request(value: str | None) -> RemainingRequest:
    group: str | None = None
    sec: int | None = None
    if value is None:
        return RemainingRequest(group=None, sec=None)

    for part in value.split(";"):
        key, separator, raw_value = part.strip().partition("=")
        if not separator:
            continue
        key = key.strip().casefold()
        raw_value = raw_value.strip()
        if key == "group" and raw_value:
            group = raw_value
        elif key == "sec":
            try:
                parsed_sec = int(raw_value)
            except ValueError:
                continue
            if parsed_sec >= 0:
                sec = parsed_sec

    return RemainingRequest(group=group, sec=sec)


def _parse_retry_after(value: str | None) -> float:
    if value is None:
        return DEFAULT_RATE_LIMIT_DELAY_SECONDS
    try:
        delay = float(value.strip())
    except ValueError:
        return DEFAULT_RATE_LIMIT_DELAY_SECONDS
    if not math.isfinite(delay) or delay < 0:
        return DEFAULT_RATE_LIMIT_DELAY_SECONDS
    return delay


class UpbitClient:
    """Shared asynchronous client for Upbit Quotation REST calls."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        max_retries: int,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = monotonic,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")

        self._attempt_timeout = min(
            timeout_seconds,
            MAX_ATTEMPT_TIMEOUT_SECONDS,
        )
        self._max_retries = min(max_retries, MAX_RETRIES)
        self._clock = clock
        self._sleeper = sleeper
        self._http_client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            timeout=httpx.Timeout(self._attempt_timeout),
            transport=transport,
            follow_redirects=False,
        )
        self._rate_limits = {
            group: _RateLimitState(lock=asyncio.Lock()) for group in RateLimitGroup
        }

    @property
    def is_closed(self) -> bool:
        return self._http_client.is_closed

    async def aclose(self) -> None:
        await self._http_client.aclose()

    async def get_markets(self) -> list[UpbitMarket]:
        return await self._request_list(
            path="v1/market/all",
            params={"is_details": "true"},
            group=RateLimitGroup.MARKET,
            adapter=TypeAdapter(list[UpbitMarket]),
        )

    async def get_tickers(
        self,
        market_codes: Sequence[str],
        *,
        max_retries: int | None = None,
    ) -> list[UpbitTicker]:
        if not market_codes:
            return []
        return await self._request_list(
            path="v1/ticker",
            params={"markets": ",".join(market_codes)},
            group=RateLimitGroup.TICKER,
            adapter=TypeAdapter(list[UpbitTicker]),
            max_retries=max_retries,
        )

    async def get_day_candles(
        self,
        market_code: str,
        *,
        max_retries: int | None = None,
    ) -> list[UpbitDayCandle]:
        return await self._request_list(
            path="v1/candles/days",
            params={"market": market_code, "count": "30"},
            group=RateLimitGroup.CANDLE,
            adapter=TypeAdapter(list[UpbitDayCandle]),
            max_retries=max_retries,
        )

    async def _request_list(
        self,
        *,
        path: str,
        params: Mapping[str, str],
        group: RateLimitGroup,
        adapter: TypeAdapter[list[ResponseT]],
        max_retries: int | None = None,
    ) -> list[ResponseT]:
        if max_retries is not None and max_retries < 0:
            raise ValueError("max_retries must not be negative")
        request_max_retries = (
            self._max_retries
            if max_retries is None
            else min(max_retries, self._max_retries)
        )
        deadline = self._clock() + TOTAL_REQUEST_BUDGET_SECONDS
        max_attempts = request_max_retries + 1
        transmissions = 0
        general_retries = 0
        rate_limit_retries = 0

        while True:
            try:
                response, retry_after = await self._send_attempt(
                    path=path,
                    params=params,
                    group=group,
                    attempt=transmissions + 1,
                    deadline=deadline,
                )
            except _RequestBudgetExceeded as exc:
                raise self._unavailable_error() from exc
            except httpx.TransportError as exc:
                transmissions += 1
                if (
                    general_retries >= request_max_retries
                    or transmissions >= max_attempts
                ):
                    raise self._unavailable_error() from exc
                delay = RETRY_BACKOFF_SECONDS[general_retries]
                general_retries += 1
                await self._sleep_for_retry(delay, deadline)
                continue

            transmissions += 1
            status_code = response.status_code

            if 200 <= status_code < 300:
                if self._clock() > deadline:
                    raise self._unavailable_error()
                return self._parse_response(response, adapter)

            if status_code == 418:
                raise self._blocked_error()

            if status_code == 429:
                if (
                    rate_limit_retries >= 1
                    or transmissions >= max_attempts
                    or not self._can_wait(retry_after, deadline)
                ):
                    raise self._rate_limited_error()
                rate_limit_retries += 1
                continue

            if 500 <= status_code < 600:
                if (
                    general_retries >= request_max_retries
                    or transmissions >= max_attempts
                ):
                    raise self._unavailable_error()
                delay = RETRY_BACKOFF_SECONDS[general_retries]
                general_retries += 1
                await self._sleep_for_retry(delay, deadline)
                continue

            if 400 <= status_code < 500:
                raise UpbitClientResponseError(
                    status_code=status_code,
                    group=group,
                )

            raise self._unavailable_error()

    async def _send_attempt(
        self,
        *,
        path: str,
        params: Mapping[str, str],
        group: RateLimitGroup,
        attempt: int,
        deadline: float,
    ) -> tuple[httpx.Response, float]:
        state = self._rate_limits[group]
        async with state.lock:
            await self._wait_for_group(state, deadline)
            remaining_budget = deadline - self._clock()
            if remaining_budget <= 0:
                raise _RequestBudgetExceeded
            attempt_timeout = min(self._attempt_timeout, remaining_budget)

            response = await self._http_client.get(
                path,
                params=params,
                timeout=httpx.Timeout(attempt_timeout),
            )
            remaining = parse_remaining_request(response.headers.get("Remaining-Req"))
            logger.info(
                "Upbit quota observed group=%s sec=%s status=%s attempt=%s",
                remaining.group or group.value,
                remaining.sec,
                response.status_code,
                attempt,
            )

            retry_after = DEFAULT_RATE_LIMIT_DELAY_SECONDS
            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                state.blocked_until = max(
                    state.blocked_until,
                    self._clock() + retry_after,
                )

            return response, retry_after

    async def _wait_for_group(
        self,
        state: _RateLimitState,
        deadline: float,
    ) -> None:
        delay = state.blocked_until - self._clock()
        if delay <= 0:
            return
        if not self._can_wait(delay, deadline):
            raise _RequestBudgetExceeded
        await self._sleeper(delay)
        if self._clock() >= deadline:
            raise _RequestBudgetExceeded

    async def _sleep_for_retry(self, delay: float, deadline: float) -> None:
        if not self._can_wait(delay, deadline):
            raise self._unavailable_error()
        await self._sleeper(delay)
        if self._clock() >= deadline:
            raise self._unavailable_error()

    def _can_wait(self, delay: float, deadline: float) -> bool:
        return delay < deadline - self._clock()

    @staticmethod
    def _parse_response(
        response: httpx.Response,
        adapter: TypeAdapter[list[ResponseT]],
    ) -> list[ResponseT]:
        try:
            payload = response.json()
            return adapter.validate_python(payload)
        except ValueError as exc:
            raise UpbitClient._unavailable_error() from exc

    @staticmethod
    def _unavailable_error() -> AppError:
        return AppError(
            code=ErrorCode.UPBIT_UNAVAILABLE,
            message=UPBIT_ERROR_MESSAGE,
        )

    @staticmethod
    def _rate_limited_error() -> AppError:
        return AppError(
            code=ErrorCode.UPBIT_RATE_LIMITED,
            message=UPBIT_ERROR_MESSAGE,
        )

    @staticmethod
    def _blocked_error() -> AppError:
        return AppError(
            code=ErrorCode.UPBIT_TEMPORARILY_BLOCKED,
            message=UPBIT_ERROR_MESSAGE,
        )


UpbitClientFactory = Callable[[Settings], UpbitClient]


def create_upbit_client(settings: Settings) -> UpbitClient:
    return UpbitClient(
        base_url=settings.upbit_base_url,
        timeout_seconds=settings.upbit_timeout_seconds,
        max_retries=settings.upbit_max_retries,
    )
