import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients.upbit import (
    MAX_ATTEMPT_TIMEOUT_SECONDS,
    RateLimitGroup,
    UpbitClient,
    UpbitClientResponseError,
    parse_remaining_request,
)
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.main import create_app


TransportHandler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


@asynccontextmanager
async def mocked_upbit_client(
    handler: TransportHandler,
    *,
    clock: FakeClock | None = None,
    base_url: str = "https://mock-upbit.example",
    timeout_seconds: float = 5,
    max_retries: int = 2,
) -> AsyncIterator[UpbitClient]:
    active_clock = clock or FakeClock()
    client = UpbitClient(
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        transport=httpx.MockTransport(handler),
        clock=active_clock,
        sleeper=active_clock.sleep,
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_endpoint_contract_models_headers_and_shared_client() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = {"Remaining-Req": "group=market; min=1800; sec=9"}
        if request.url.path == "/v1/market/all":
            return httpx.Response(
                200,
                headers=headers,
                json=[
                    {
                        "market": "KRW-BTC",
                        "korean_name": "비트코인",
                        "english_name": "Bitcoin",
                        "market_event": {"warning": False, "caution": {}},
                        "unused": "ignored",
                    }
                ],
            )
        if request.url.path == "/v1/ticker":
            return httpx.Response(
                200,
                headers=headers,
                json=[
                    {
                        "market": "KRW-BTC",
                        "trade_price": 142300000,
                        "signed_change_rate": 0.0125,
                        "unused": 1,
                    }
                ],
            )
        return httpx.Response(
            200,
            headers=headers,
            json=[
                {
                    "candle_date_time_kst": "2026-07-25T09:00:00",
                    "trade_price": 142300000,
                    "unused": True,
                }
            ],
        )

    async with mocked_upbit_client(
        handler,
        base_url="https://custom-upbit.example",
    ) as client:
        http_client_id = id(client._http_client)
        markets = await client.get_markets()
        tickers = await client.get_tickers(["KRW-BTC", "KRW-ETH"])
        candles = await client.get_day_candles("KRW-BTC")
        assert id(client._http_client) == http_client_id

    assert len(requests) == 3
    assert all(request.url.host == "custom-upbit.example" for request in requests)
    assert requests[0].url.path == "/v1/market/all"
    assert requests[0].url.params["is_details"] == "true"
    assert requests[1].url.path == "/v1/ticker"
    assert requests[1].url.params["markets"] == "KRW-BTC,KRW-ETH"
    assert requests[2].url.path == "/v1/candles/days"
    assert requests[2].url.params["market"] == "KRW-BTC"
    assert requests[2].url.params["count"] == "30"
    assert all("origin" not in request.headers for request in requests)
    assert all("authorization" not in request.headers for request in requests)
    assert all("access_key" not in request.url.params for request in requests)

    assert markets[0].market == "KRW-BTC"
    assert markets[0].market_event.warning is False
    assert tickers[0].trade_price == Decimal("142300000")
    assert tickers[0].signed_change_rate == Decimal("0.0125")
    assert candles[0].candle_date_time_kst == "2026-07-25T09:00:00"
    assert candles[0].trade_price == Decimal("142300000")


@pytest.mark.asyncio
async def test_empty_ticker_list_does_not_send_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    async with mocked_upbit_client(handler) as client:
        result = await client.get_tickers([])

    assert result == []
    assert requests == []


@pytest.mark.asyncio
async def test_timeout_uses_setting_remaining_budget_and_five_second_cap() -> None:
    clock = FakeClock()
    seen_timeouts: list[dict[str, float]] = []
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        seen_timeouts.append(request.extensions["timeout"])
        if requests == 1:
            clock.advance(4)
            return httpx.Response(500)
        return httpx.Response(200, json=[])

    async with mocked_upbit_client(
        handler,
        clock=clock,
        timeout_seconds=9,
    ) as client:
        await client.get_markets()

    assert seen_timeouts[0] == {
        "connect": MAX_ATTEMPT_TIMEOUT_SECONDS,
        "read": MAX_ATTEMPT_TIMEOUT_SECONDS,
        "write": MAX_ATTEMPT_TIMEOUT_SECONDS,
        "pool": MAX_ATTEMPT_TIMEOUT_SECONDS,
    }
    assert seen_timeouts[1] == {
        "connect": 3.5,
        "read": 3.5,
        "write": 3.5,
        "pool": 3.5,
    }
    assert clock.sleeps == [0.5]


@pytest.mark.asyncio
async def test_attempt_timeout_uses_configured_value_below_cap() -> None:
    seen_timeout: dict[str, float] | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_timeout
        seen_timeout = request.extensions["timeout"]
        return httpx.Response(200, json=[])

    async with mocked_upbit_client(handler, timeout_seconds=3.25) as client:
        await client.get_markets()

    assert seen_timeout == {
        "connect": 3.25,
        "read": 3.25,
        "write": 3.25,
        "pool": 3.25,
    }


@pytest.mark.parametrize(
    "payload",
    [
        b"private malformed json",
        {"market": "KRW-BTC"},
        [{"market": "KRW-BTC"}],
        [
            {
                "market": 123,
                "korean_name": "비트코인",
                "english_name": "Bitcoin",
                "market_event": {"warning": False, "caution": {}},
            }
        ],
        [
            {
                "market": "KRW-BTC",
                "korean_name": "비트코인",
                "english_name": "Bitcoin",
                "market_event": {"warning": "false", "caution": {}},
            }
        ],
        [
            {
                "market": "KRW-BTC",
                "korean_name": "비트코인",
                "english_name": "Bitcoin",
                "market_event": {"warning": False, "caution": {"REASON": 1}},
            }
        ],
    ],
)
@pytest.mark.asyncio
async def test_malformed_success_response_fails_without_retry_or_body_leak(
    payload: bytes | object,
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if isinstance(payload, bytes):
            return httpx.Response(200, content=payload)
        return httpx.Response(200, json=payload)

    async with mocked_upbit_client(handler) as client:
        with pytest.raises(AppError) as exc_info:
            await client.get_markets()

    assert requests == 1
    assert exc_info.value.code == ErrorCode.UPBIT_UNAVAILABLE
    assert exc_info.value.status_code == 502
    assert "private malformed json" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_numeric_strings_are_not_accepted_as_upbit_numbers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "market": "KRW-BTC",
                    "trade_price": "142300000",
                    "signed_change_rate": 0.0125,
                }
            ],
        )

    async with mocked_upbit_client(handler) as client:
        with pytest.raises(AppError) as exc_info:
            await client.get_tickers(["KRW-BTC"])

    assert exc_info.value.code == ErrorCode.UPBIT_UNAVAILABLE


@pytest.mark.asyncio
async def test_invalid_candle_timestamp_is_not_accepted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "candle_date_time_kst": "not-a-timestamp",
                    "trade_price": 142300000,
                }
            ],
        )

    async with mocked_upbit_client(handler) as client:
        with pytest.raises(AppError) as exc_info:
            await client.get_day_candles("KRW-BTC")

    assert exc_info.value.code == ErrorCode.UPBIT_UNAVAILABLE


@pytest.mark.parametrize(
    ("header", "expected_group", "expected_sec"),
    [
        ("group=market; min=1800; sec=9", "market", 9),
        (" sec = 7 ; unknown=x ; group = ticker ; min=1 ", "ticker", 7),
        ("min=1800", None, None),
        ("group=candle; sec=-1", "candle", None),
        ("group=candle; sec=not-a-number", "candle", None),
        ("malformed", None, None),
        (None, None, None),
    ],
)
def test_remaining_request_parser(
    header: str | None,
    expected_group: str | None,
    expected_sec: int | None,
) -> None:
    parsed = parse_remaining_request(header)

    assert parsed.group == expected_group
    assert parsed.sec == expected_sec


@pytest.mark.asyncio
async def test_malformed_remaining_request_does_not_fail_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Remaining-Req": "group; sec=broken; min=999999"},
            json=[],
        )

    async with mocked_upbit_client(handler) as client:
        assert await client.get_markets() == []


@pytest.mark.parametrize("retry_after", [None, "-1", "nan", "inf", "invalid"])
@pytest.mark.asyncio
async def test_429_missing_or_invalid_retry_after_uses_one_second(
    retry_after: str | None,
) -> None:
    clock = FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            headers = {} if retry_after is None else {"Retry-After": retry_after}
            return httpx.Response(429, headers=headers)
        return httpx.Response(200, json=[])

    async with mocked_upbit_client(handler, clock=clock) as client:
        assert await client.get_markets() == []

    assert requests == 2
    assert clock.sleeps == [1]


@pytest.mark.asyncio
async def test_429_waits_retry_after_once_then_maps_second_429() -> None:
    clock = FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "2.5"},
            content=b"private upbit rate body",
        )

    async with mocked_upbit_client(handler, clock=clock) as client:
        with pytest.raises(AppError) as exc_info:
            await client.get_markets()

    assert requests == 2
    assert clock.sleeps == [2.5]
    assert exc_info.value.code == ErrorCode.UPBIT_RATE_LIMITED
    assert exc_info.value.status_code == 503
    assert "private upbit rate body" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_long_retry_after_does_not_wait_past_budget() -> None:
    clock = FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429, headers={"Retry-After": "20"})

    async with mocked_upbit_client(handler, clock=clock) as client:
        with pytest.raises(AppError) as exc_info:
            await client.get_markets()

    assert requests == 1
    assert clock.now == 0
    assert clock.sleeps == []
    assert exc_info.value.code == ErrorCode.UPBIT_RATE_LIMITED


@pytest.mark.asyncio
async def test_rate_limit_state_is_reused_by_same_group() -> None:
    clock = FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests <= 2:
            return httpx.Response(429)
        return httpx.Response(200, json=[])

    async with mocked_upbit_client(handler, clock=clock) as client:
        with pytest.raises(AppError):
            await client.get_markets()
        assert await client.get_markets() == []

    assert requests == 3
    assert clock.sleeps == [1, 1]


@pytest.mark.asyncio
async def test_418_is_never_retried_and_maps_to_blocked() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(418, content=b"private temporary block body")

    async with mocked_upbit_client(handler) as client:
        with pytest.raises(AppError) as exc_info:
            await client.get_markets()

    assert requests == 1
    assert exc_info.value.code == ErrorCode.UPBIT_TEMPORARILY_BLOCKED
    assert exc_info.value.status_code == 503
    assert "private temporary block body" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_5xx_uses_half_and_one_second_backoffs_then_stops() -> None:
    clock = FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500 + requests, content=b"private server error")

    async with mocked_upbit_client(handler, clock=clock) as client:
        with pytest.raises(AppError) as exc_info:
            await client.get_markets()

    assert requests == 3
    assert clock.sleeps == [0.5, 1]
    assert exc_info.value.code == ErrorCode.UPBIT_UNAVAILABLE
    assert exc_info.value.status_code == 502
    assert "private server error" not in str(exc_info.value)


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
@pytest.mark.asyncio
async def test_transport_errors_retry_twice_then_map_to_unavailable(
    error_type: type[httpx.TransportError],
) -> None:
    clock = FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise error_type("private transport detail", request=request)

    async with mocked_upbit_client(handler, clock=clock) as client:
        with pytest.raises(AppError) as exc_info:
            await client.get_markets()

    assert requests == 3
    assert clock.sleeps == [0.5, 1]
    assert exc_info.value.code == ErrorCode.UPBIT_UNAVAILABLE
    assert "private transport detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_configured_retry_count_is_capped_at_two() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with mocked_upbit_client(handler, max_retries=99) as client:
        with pytest.raises(AppError):
            await client.get_markets()

    assert requests == 3


@pytest.mark.asyncio
async def test_zero_configured_retries_sends_only_initial_request() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with mocked_upbit_client(handler, max_retries=0) as client:
        with pytest.raises(AppError):
            await client.get_markets()

    assert requests == 1


@pytest.mark.asyncio
async def test_day_candle_request_override_zero_disables_retries() -> None:
    clock = FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.path == "/v1/candles/days"
        assert dict(request.url.params) == {"market": "KRW-BTC", "count": "30"}
        return httpx.Response(500)

    async with mocked_upbit_client(handler, clock=clock) as client:
        with pytest.raises(AppError) as captured:
            await client.get_day_candles("KRW-BTC", max_retries=0)

    assert captured.value.code is ErrorCode.UPBIT_UNAVAILABLE
    assert requests == 1
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_day_candle_default_keeps_documented_retry_policy() -> None:
    clock = FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with mocked_upbit_client(handler, clock=clock) as client:
        with pytest.raises(AppError) as captured:
            await client.get_day_candles("KRW-BTC")

    assert captured.value.code is ErrorCode.UPBIT_UNAVAILABLE
    assert requests == 3
    assert clock.sleeps == [0.5, 1]


@pytest.mark.asyncio
async def test_mixed_retryable_errors_never_exceed_three_transmissions() -> None:
    clock = FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(500)
        if requests == 2:
            return httpx.Response(429)
        return httpx.Response(500)

    async with mocked_upbit_client(handler, clock=clock) as client:
        with pytest.raises(AppError) as exc_info:
            await client.get_markets()

    assert requests == 3
    assert clock.sleeps == [0.5, 1]
    assert exc_info.value.code == ErrorCode.UPBIT_UNAVAILABLE


@pytest.mark.asyncio
async def test_retry_stops_when_backoff_would_exhaust_budget() -> None:
    clock = FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        clock.advance(7.75)
        return httpx.Response(500)

    async with mocked_upbit_client(handler, clock=clock) as client:
        with pytest.raises(AppError) as exc_info:
            await client.get_markets()

    assert requests == 1
    assert clock.now == 7.75
    assert clock.sleeps == []
    assert exc_info.value.code == ErrorCode.UPBIT_UNAVAILABLE


@pytest.mark.asyncio
async def test_total_budget_never_advances_past_eight_seconds() -> None:
    clock = FakeClock()
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            clock.advance(5)
        else:
            clock.advance(2.5)
        return httpx.Response(500)

    async with mocked_upbit_client(handler, clock=clock) as client:
        with pytest.raises(AppError):
            await client.get_markets()

    assert requests == 2
    assert clock.sleeps == [0.5]
    assert clock.now == 8


@pytest.mark.asyncio
async def test_other_4xx_is_internal_safe_exception_without_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests = 0
    private_body = "private four hundred response"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            404,
            headers={"X-Private": "private-header"},
            content=private_body.encode(),
        )

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        async with mocked_upbit_client(handler) as client:
            with pytest.raises(UpbitClientResponseError) as exc_info:
                await client.get_markets()

    assert requests == 1
    assert exc_info.value.status_code == 404
    assert exc_info.value.group == RateLimitGroup.MARKET
    assert private_body not in str(exc_info.value)
    assert private_body not in caplog.text
    assert "private-header" not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_is_not_converted_or_retried() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise asyncio.CancelledError

    async with mocked_upbit_client(handler) as client:
        with pytest.raises(asyncio.CancelledError):
            await client.get_markets()

    assert requests == 1


def test_lifespan_creates_one_client_health_is_lazy_and_shutdown_closes() -> None:
    requests: list[httpx.Request] = []
    created_clients: list[UpbitClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    def factory(settings: Settings) -> UpbitClient:
        client = UpbitClient(
            base_url=settings.upbit_base_url,
            timeout_seconds=settings.upbit_timeout_seconds,
            max_retries=settings.upbit_max_retries,
            transport=httpx.MockTransport(handler),
        )
        created_clients.append(client)
        return client

    application = create_app(
        Settings(_env_file=None),
        upbit_client_factory=factory,
        load_markets_on_startup=False,
    )
    assert application.state.upbit_client is None

    with TestClient(application) as client:
        shared_client = application.state.upbit_client
        first = client.get("/api/health")
        second = client.get("/api/health")
        assert application.state.upbit_client is shared_client
        assert not shared_client.is_closed

    assert first.json() == {"data": {"status": "ok"}, "meta": None}
    assert second.status_code == 200
    assert len(created_clients) == 1
    assert requests == []
    assert created_clients[0].is_closed


def test_upbit_close_failure_does_not_skip_existing_lifespan_cleanup() -> None:
    class CloseFailingUpbitClient(UpbitClient):
        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("safe close failure")

    class SupabaseClientStub:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    def factory(settings: Settings) -> UpbitClient:
        return CloseFailingUpbitClient(
            base_url=settings.upbit_base_url,
            timeout_seconds=settings.upbit_timeout_seconds,
            max_retries=settings.upbit_max_retries,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
        )

    application = create_app(
        Settings(_env_file=None),
        upbit_client_factory=factory,
        load_markets_on_startup=False,
    )
    supabase_client = SupabaseClientStub()
    application.state.supabase_http_client = supabase_client

    with TestClient(application) as client:
        assert client.get("/api/health").status_code == 200

    assert supabase_client.closed


def test_expected_upbit_error_uses_common_envelope_not_internal_500() -> None:
    private_body = "private upbit block message"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, content=private_body.encode())

    def factory(settings: Settings) -> UpbitClient:
        return UpbitClient(
            base_url=settings.upbit_base_url,
            timeout_seconds=settings.upbit_timeout_seconds,
            max_retries=settings.upbit_max_retries,
            transport=httpx.MockTransport(handler),
        )

    application = create_app(
        Settings(_env_file=None),
        upbit_client_factory=factory,
        load_markets_on_startup=False,
    )

    @application.get("/test-upbit-error")
    async def upbit_error() -> None:
        await application.state.upbit_client.get_markets()

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/test-upbit-error")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "UPBIT_TEMPORARILY_BLOCKED",
            "message": "시세 정보를 일시적으로 조회할 수 없습니다.",
            "details": None,
        }
    }
    assert private_body not in response.text


def test_production_router_still_has_no_upbit_test_endpoint() -> None:
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    api_paths = {
        route.path for route in application.routes if route.path.startswith("/api")
    }

    assert api_paths == {
        "/api/health",
        "/api/coins/search",
        "/api/coins/{marketCode}/chart",
        "/api/paper/account",
        "/api/paper/top-ups",
    }
