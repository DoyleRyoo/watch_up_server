import json
from collections.abc import Sequence
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_auth_context
from app.cache.keys import MARKET_LIST_KEY
from app.cache.redis import RedisCache
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.exceptions import AuthenticationError
from app.main import create_app
from app.models.auth import AuthContext
from app.models.market import Market, MarketStatus
from app.schemas.upbit import UpbitMarket
from app.services.market_list import MarketListService
from tests.test_market_list_service import FakeMarketSource
from tests.test_redis_cache import FakeRedis


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


def _authenticated_context() -> AuthContext:
    return AuthContext(user_id=uuid4(), access_token="synthetic-test-token")


def _search_app(
    markets: Sequence[Market],
) -> tuple[FastAPI, MarketListService, FakeRedis, FakeMarketSource]:
    fake_redis = FakeRedis()
    fake_redis.values[MARKET_LIST_KEY] = json.dumps(
        [market.to_cache_value() for market in markets]
    )
    cache = RedisCache(fake_redis)
    source = FakeMarketSource()
    service = MarketListService(upbit_client=source, redis_cache=cache)
    application = create_app(
        Settings(_env_file=None),
        redis_cache_factory=lambda settings: cache,
        market_list_service_factory=lambda upbit, redis: service,
        load_markets_on_startup=False,
    )
    return application, service, fake_redis, source


def _valid_search_app(markets: Sequence[Market]) -> tuple[FastAPI, FakeRedis]:
    application, _, fake_redis, _ = _search_app(markets)
    application.dependency_overrides[get_auth_context] = _authenticated_context
    return application, fake_redis


def _assert_auth_error(response: httpx.Response, code: str) -> None:
    assert response.status_code == 401
    assert response.json()["error"]["code"] == code
    assert response.headers["www-authenticate"] == "Bearer"


def test_search_endpoint_requires_auth_before_cache_or_upbit() -> None:
    application, _, fake_redis, source = _search_app(
        [_market("KRW-BTC", "비트코인", "Bitcoin")]
    )

    with TestClient(application) as client:
        response = client.get("/api/coins/search?query=btc")
        assert fake_redis.commands == []
        assert source.calls == 0

    _assert_auth_error(response, "AUTH_REQUIRED")


def test_expired_auth_does_not_reach_search_service() -> None:
    application, _, fake_redis, source = _search_app(
        [_market("KRW-BTC", "비트코인", "Bitcoin")]
    )

    def expired() -> AuthContext:
        raise AuthenticationError.expired()

    application.dependency_overrides[get_auth_context] = expired
    with TestClient(application) as client:
        response = client.get("/api/coins/search?query=btc")
        assert fake_redis.commands == []
        assert source.calls == 0

    _assert_auth_error(response, "AUTH_TOKEN_EXPIRED")


@pytest.mark.parametrize("path", ["/api/coins/search", "/api/coins/search?query="])
def test_missing_and_empty_query_use_common_invalid_request(path: str) -> None:
    application, _ = _valid_search_app([])

    with TestClient(application) as client:
        response = client.get(path)

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "요청값이 올바르지 않습니다.",
            "details": None,
        }
    }


def test_whitespace_query_is_invalid_request() -> None:
    application, _ = _valid_search_app([])

    with TestClient(application) as client:
        response = client.get("/api/coins/search", params={"query": "   "})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_search_response_is_camel_case_trimmed_and_has_only_contract_fields() -> None:
    application, _ = _valid_search_app(
        [
            _market("KRW-BTC", "비트코인", "Bitcoin"),
            _market(
                "KRW-BCH",
                "비트코인캐시",
                "Bitcoin Cash",
                MarketStatus.CAUTION,
            ),
        ]
    )

    with TestClient(application) as client:
        response = client.get(
            "/api/coins/search",
            params={"query": "  bitcoin  "},
        )

    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                "marketCode": "KRW-BTC",
                "koreanName": "비트코인",
                "englishName": "Bitcoin",
                "status": "ACTIVE",
            },
            {
                "marketCode": "KRW-BCH",
                "koreanName": "비트코인캐시",
                "englishName": "Bitcoin Cash",
                "status": "CAUTION",
            },
        ],
        "meta": {"count": 2},
    }
    assert set(response.json()["data"][0]) == {
        "marketCode",
        "koreanName",
        "englishName",
        "status",
    }
    assert "isStale" not in response.text
    assert "UNAVAILABLE" not in response.text
    assert "PRICE_ERROR" not in response.text


def test_no_search_result_is_200_with_empty_list_and_zero_count() -> None:
    application, _ = _valid_search_app([_market("KRW-BTC", "비트코인", "Bitcoin")])

    with TestClient(application) as client:
        response = client.get("/api/coins/search?query=not-found")

    assert response.status_code == 200
    assert response.json() == {"data": [], "meta": {"count": 0}}


def test_search_response_limit_and_meta_count_are_both_twenty() -> None:
    markets = [
        _market(f"KRW-C{index:02d}", f"코인{index:02d}", f"Coin {index:02d}")
        for index in range(25)
    ]
    application, _ = _valid_search_app(markets)

    with TestClient(application) as client:
        response = client.get("/api/coins/search?query=coin")

    assert response.status_code == 200
    assert len(response.json()["data"]) == 20
    assert response.json()["meta"] == {"count": 20}


class LifespanMarketSource:
    def __init__(self, *, error: AppError | None = None) -> None:
        self.error = error
        self.calls = 0
        self.closed = False

    async def get_markets(self) -> list[UpbitMarket]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [
            UpbitMarket(
                market="KRW-BTC",
                korean_name="비트코인",
                english_name="Bitcoin",
                market_event={"warning": False, "caution": {}},
            )
        ]

    async def aclose(self) -> None:
        self.closed = True


def test_startup_loads_once_reuses_service_and_health_sends_no_commands() -> None:
    fake_redis = FakeRedis()
    cache = RedisCache(fake_redis)
    source = LifespanMarketSource()
    application = create_app(
        Settings(_env_file=None),
        upbit_client_factory=lambda settings: source,
        redis_cache_factory=lambda settings: cache,
    )
    application.dependency_overrides[get_auth_context] = _authenticated_context

    with TestClient(application) as client:
        service = application.state.market_list_service
        assert source.calls == 1
        commands_after_startup = list(fake_redis.commands)
        health = client.get("/api/health")
        assert fake_redis.commands == commands_after_startup
        search = client.get("/api/coins/search?query=btc")
        assert application.state.market_list_service is service
        assert source.calls == 1

    assert health.status_code == 200
    assert health.json() == {"data": {"status": "ok"}, "meta": None}
    assert search.status_code == 200
    assert source.closed is True


def test_typed_startup_failure_does_not_stop_application_or_health() -> None:
    failure = AppError(
        code=ErrorCode.UPBIT_UNAVAILABLE,
        message="safe startup failure",
    )
    source = LifespanMarketSource(error=failure)
    fake_redis = FakeRedis()
    application = create_app(
        Settings(_env_file=None),
        upbit_client_factory=lambda settings: source,
        redis_cache_factory=lambda settings: RedisCache(fake_redis),
    )

    with TestClient(application) as client:
        commands_after_startup = list(fake_redis.commands)
        response = client.get("/api/health")
        assert fake_redis.commands == commands_after_startup

    assert source.calls == 1
    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}, "meta": None}


def test_unexpected_startup_programming_error_is_not_hidden() -> None:
    class BrokenService:
        async def get_markets(self) -> tuple[Market, ...]:
            raise RuntimeError("programming failure")

    source = LifespanMarketSource()
    application = create_app(
        Settings(_env_file=None),
        upbit_client_factory=lambda settings: source,
        market_list_service_factory=lambda upbit, redis: BrokenService(),
    )

    with pytest.raises(RuntimeError, match="programming failure"):
        with TestClient(application):
            pass
