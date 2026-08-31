from collections.abc import Iterator, Sequence
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_auth_context, get_supabase_client
from app.api.dependencies.services import get_price_service
from app.cache.redis import RedisCache
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.exceptions import AuthenticationError
from app.main import create_app
from app.models.auth import AuthContext
from app.models.market import Market, MarketStatus
from app.schemas.chart import ChartDataResponse
from app.schemas.upbit import UpbitDayCandle
from app.services.chart import ChartService
from app.services.price import DisplayPrice, PriceStatus
from tests.test_chart_service import FakeCandleSource, FakeMarketListService, candle
from tests.test_redis_cache import FakeRedis


BTC = Market(
    market_code="KRW-BTC",
    korean_name="비트코인",
    english_name="Bitcoin",
    status=MarketStatus.ACTIVE,
)


class FakeDisplayPriceService:
    def __init__(self, price: DisplayPrice) -> None:
        self.price = price
        self.calls: list[str] = []

    async def get_display_price(self, market_code: str) -> DisplayPrice:
        self.calls.append(market_code)
        return self.price


FRESH_PRICE = DisplayPrice(Decimal("142300000.25"), PriceStatus.FRESH)


def authenticated_context() -> AuthContext:
    return AuthContext(user_id=uuid4(), access_token="synthetic-test-token")


def chart_app(
    candles: Sequence[UpbitDayCandle] = (),
    *,
    source_error: BaseException | None = None,
    market: Market | None = BTC,
    display_price: DisplayPrice = FRESH_PRICE,
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
    application.dependency_overrides[get_price_service] = lambda: (
        FakeDisplayPriceService(display_price)
    )
    return application, market_service, source, fake_redis


def valid_client(
    candles: Sequence[UpbitDayCandle] = (),
    *,
    source_error: BaseException | None = None,
    market: Market | None = BTC,
    display_price: DisplayPrice = FRESH_PRICE,
) -> Iterator[tuple[TestClient, FastAPI, FakeMarketListService, FakeCandleSource]]:
    application, market_service, source, _ = chart_app(
        candles,
        source_error=source_error,
        market=market,
        display_price=display_price,
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
            "koreanName": "비트코인",
            "englishName": "Bitcoin",
            "marketStatus": "ACTIVE",
            "currentPrice": "142300000.25",
            "priceStatus": "FRESH",
            "period": "30d",
            "candles": [
                {"date": "2026-06-01", "closingPrice": "0"},
                {"date": "2026-06-02", "closingPrice": "99"},
                {"date": "2026-06-03", "closingPrice": "100.125"},
            ],
        },
        "meta": {"count": 3},
    }
    assert set(response.json()["data"]) == {
        "marketCode",
        "koreanName",
        "englishName",
        "marketStatus",
        "currentPrice",
        "priceStatus",
        "period",
        "candles",
    }
    assert set(response.json()["data"]["candles"][0]) == {
        "date",
        "closingPrice",
    }
    assert market_service.calls == ["KRW-BTC"]
    assert source.calls == [("KRW-BTC", None)]
    assert "market_code" not in response.text
    assert "closing_price" not in response.text
    assert "isStale" not in response.text
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
        "data": {
            "marketCode": "KRW-BTC",
            "koreanName": "비트코인",
            "englishName": "Bitcoin",
            "marketStatus": "ACTIVE",
            "currentPrice": "142300000.25",
            "priceStatus": "FRESH",
            "period": "30d",
            "candles": [],
        },
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
    "error_code",
    [
        ErrorCode.UPBIT_UNAVAILABLE,
        ErrorCode.UPBIT_RATE_LIMITED,
        ErrorCode.UPBIT_TEMPORARILY_BLOCKED,
    ],
)
def test_candle_errors_return_empty_chart_without_hiding_price(
    error_code: ErrorCode,
) -> None:
    failure = AppError(code=error_code, message="safe chart error")
    client_context = valid_client(source_error=failure)
    client, _, _, _ = next(client_context)
    try:
        response = client.get("/api/coins/KRW-BTC/chart")
    finally:
        client_context.close()

    assert response.status_code == 200
    assert response.json()["data"]["candles"] == []
    assert response.json()["data"]["currentPrice"] == "142300000.25"
    assert response.json()["data"]["priceStatus"] == "FRESH"
    assert response.json()["meta"] == {"count": 0}


def test_unavailable_market_keeps_stale_price_status_independent() -> None:
    unavailable = BTC.model_copy(update={"status": MarketStatus.UNAVAILABLE})
    stale = DisplayPrice(Decimal("140000000"), PriceStatus.STALE)
    client_context = valid_client(
        [candle(1, 1)], market=unavailable, display_price=stale
    )
    client, _, _, _ = next(client_context)
    try:
        response = client.get("/api/coins/KRW-BTC/chart")
    finally:
        client_context.close()

    assert response.status_code == 200
    assert response.json()["data"]["marketStatus"] == "UNAVAILABLE"
    assert response.json()["data"]["currentPrice"] == "140000000"
    assert response.json()["data"]["priceStatus"] == "STALE"


def test_price_failure_returns_null_and_price_error() -> None:
    failed = DisplayPrice(None, PriceStatus.PRICE_ERROR)
    client_context = valid_client([candle(1, 1)], display_price=failed)
    client, _, _, _ = next(client_context)
    try:
        response = client.get("/api/coins/KRW-BTC/chart")
    finally:
        client_context.close()

    assert response.status_code == 200
    assert response.json()["data"]["currentPrice"] is None
    assert response.json()["data"]["priceStatus"] == "PRICE_ERROR"
    assert response.json()["data"]["candles"] == [
        {"date": "2026-06-01", "closingPrice": "1"}
    ]


def test_chart_schema_rejects_inconsistent_price_error_pair() -> None:
    with pytest.raises(ValueError):
        ChartDataResponse(
            market_code="KRW-BTC",
            korean_name="비트코인",
            english_name="Bitcoin",
            market_status=MarketStatus.ACTIVE,
            current_price=None,
            price_status=PriceStatus.FRESH,
            candles=[],
        )


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
