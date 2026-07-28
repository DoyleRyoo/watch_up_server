from collections.abc import Iterator, Sequence
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_auth_context, get_supabase_client
from app.cache.redis import RedisCache
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.exceptions import AuthenticationError
from app.main import create_app
from app.models.auth import AuthContext
from app.models.market import Market, MarketStatus
from app.schemas.upbit import UpbitDayCandle
from app.services.chart import ChartService
from tests.test_chart_service import FakeCandleSource, FakeMarketListService, candle
from tests.test_redis_cache import FakeRedis


BTC = Market(
    market_code="KRW-BTC",
    korean_name="비트코인",
    english_name="Bitcoin",
    status=MarketStatus.ACTIVE,
)


def authenticated_context() -> AuthContext:
    return AuthContext(user_id=uuid4(), access_token="synthetic-test-token")


def chart_app(
    candles: Sequence[UpbitDayCandle] = (),
    *,
    source_error: BaseException | None = None,
    market: Market | None = BTC,
) -> tuple[FastAPI, FakeMarketListService, FakeCandleSource, FakeRedis]:
    fake_redis = FakeRedis()
    cache = RedisCache(fake_redis)
    market_service = FakeMarketListService(market)
    source = FakeCandleSource(candles, error=source_error)
    service = ChartService(
        upbit_client=source,
        redis_cache=cache,
        market_list_service=market_service,  # type: ignore[arg-type]
    )
    application = create_app(
        Settings(_env_file=None),
        redis_cache_factory=lambda settings: cache,
        chart_service_factory=lambda upbit, redis, markets: service,
        load_markets_on_startup=False,
    )
    return application, market_service, source, fake_redis


def valid_client(
    candles: Sequence[UpbitDayCandle] = (),
    *,
    source_error: BaseException | None = None,
    market: Market | None = BTC,
) -> Iterator[tuple[TestClient, FastAPI, FakeMarketListService, FakeCandleSource]]:
    application, market_service, source, _ = chart_app(
        candles,
        source_error=source_error,
        market=market,
    )
    application.dependency_overrides[get_auth_context] = authenticated_context
    with TestClient(application) as client:
        yield client, application, market_service, source


def assert_auth_error(response: httpx.Response, code: str) -> None:
    assert response.status_code == 401
    assert response.json()["error"]["code"] == code
    assert response.headers["www-authenticate"] == "Bearer"


def test_chart_requires_auth_before_market_cache_and_upbit() -> None:
    application, market_service, source, redis = chart_app([candle(1, 1)])

    with TestClient(application) as client:
        response = client.get("/api/coins/KRW-BTC/chart")
        assert market_service.calls == []
        assert source.calls == []
        assert redis.commands == []

    assert_auth_error(response, "AUTH_REQUIRED")


def test_expired_auth_stops_chart_service() -> None:
    application, market_service, source, redis = chart_app([candle(1, 1)])

    def expired() -> AuthContext:
        raise AuthenticationError.expired()

    application.dependency_overrides[get_auth_context] = expired
    with TestClient(application) as client:
        response = client.get("/api/coins/KRW-BTC/chart")
        assert market_service.calls == []
        assert source.calls == []
        assert redis.commands == []

    assert_auth_error(response, "AUTH_TOKEN_EXPIRED")


@pytest.mark.parametrize(
    "market_code",
    [
        "BTC",
        "USD-BTC",
        "krw-btc",
        "KRW-",
        "KRW-BTC%20",
        "KRW-BTC!",
        "KRW-ABCDEFGHIJKLMNOPQ",
    ],
)
def test_invalid_market_code_uses_common_400_not_framework_422(
    market_code: str,
) -> None:
    client_context = valid_client([candle(1, 1)])
    client, _, market_service, source = next(client_context)
    try:
        response = client.get(f"/api/coins/{market_code}/chart")
    finally:
        client_context.close()

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "INVALID_MARKET_CODE",
            "message": "유효하지 않은 마켓 코드입니다.",
            "details": None,
        }
    }
    assert market_service.calls == []
    assert source.calls == []
    assert "detail" not in response.json()


def test_valid_response_is_sorted_camel_case_and_contract_only() -> None:
    client_context = valid_client([candle(3, 100.125), candle(1, 0), candle(2, 99)])
    client, application, market_service, source = next(client_context)
    try:
        response = client.get("/api/coins/KRW-BTC/chart")
    finally:
        client_context.close()

    assert response.status_code == 200
    assert response.json() == {
        "data": {
            "marketCode": "KRW-BTC",
            "period": "30d",
            "candles": [
                {"date": "2026-06-01", "closingPrice": 0},
                {"date": "2026-06-02", "closingPrice": 99},
                {"date": "2026-06-03", "closingPrice": 100.125},
            ],
        },
        "meta": {"count": 3},
    }
    assert set(response.json()["data"]) == {"marketCode", "period", "candles"}
    assert set(response.json()["data"]["candles"][0]) == {
        "date",
        "closingPrice",
    }
    assert market_service.calls == ["KRW-BTC"]
    assert source.calls == [("KRW-BTC", None)]
    assert "market_code" not in response.text
    assert "closing_price" not in response.text
    assert "isStale" not in response.text
    assert "status" not in response.json()["data"]
    assert "currentPrice" not in response.text
    assert application.state.supabase_http_client is None


def test_empty_chart_is_200_with_count_zero() -> None:
    client_context = valid_client([])
    client, _, _, _ = next(client_context)
    try:
        response = client.get("/api/coins/KRW-BTC/chart")
    finally:
        client_context.close()

    assert response.status_code == 200
    assert response.json() == {
        "data": {"marketCode": "KRW-BTC", "period": "30d", "candles": []},
        "meta": {"count": 0},
    }


def test_chart_does_not_resolve_supabase_dependency() -> None:
    client_context = valid_client([candle(1, 1)])
    client, application, _, _ = next(client_context)

    def forbidden_supabase() -> None:
        raise AssertionError("chart must not create a user Supabase client")

    application.dependency_overrides[get_supabase_client] = forbidden_supabase
    try:
        response = client.get("/api/coins/KRW-BTC/chart")
    finally:
        client_context.close()

    assert response.status_code == 200
    assert application.state.supabase_http_client is None


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        (ErrorCode.UPBIT_UNAVAILABLE, 502),
        (ErrorCode.UPBIT_RATE_LIMITED, 503),
        (ErrorCode.UPBIT_TEMPORARILY_BLOCKED, 503),
    ],
)
def test_chart_errors_use_common_safe_envelope(
    error_code: ErrorCode,
    expected_status: int,
) -> None:
    failure = AppError(code=error_code, message="safe chart error")
    client_context = valid_client(source_error=failure)
    client, _, _, _ = next(client_context)
    try:
        response = client.get("/api/coins/KRW-BTC/chart")
    finally:
        client_context.close()

    assert response.status_code == expected_status
    assert response.json() == {
        "error": {
            "code": error_code.value,
            "message": "safe chart error",
            "details": None,
        }
    }


def test_chart_openapi_preserves_market_code_name_and_response_model() -> None:
    application, _, _, _ = chart_app()
    schema = application.openapi()
    operation = schema["paths"]["/api/coins/{marketCode}/chart"]["get"]

    assert operation["parameters"][0]["name"] == "marketCode"
    assert operation["parameters"][0]["in"] == "path"
    response_schema = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert response_schema["$ref"].endswith("/ChartResponse")
