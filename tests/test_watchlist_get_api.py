from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_auth_context, get_supabase_client
from app.api.dependencies.services import (
    get_market_list_service,
    get_price_service,
    get_watchlist_service,
)
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.exceptions import AuthenticationError
from app.main import create_app
from app.models.auth import AuthContext
from app.models.market import MarketStatus
from app.repositories.watchlist import WatchlistRepositoryError
from app.services.watchlist import WatchlistService
from tests.test_watchlist_query_service import (
    BASE_TIME,
    CLIENT,
    USER_ID,
    FakeListRepository,
    FakeMarketListService,
    FakePriceService,
    market,
    price,
    row,
)


OTHER_USER_ID = uuid4()


def query_app(
    repository: FakeListRepository,
    *,
    market_service: FakeMarketListService | None = None,
    price_service: FakePriceService | None = None,
) -> FastAPI:
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    application.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id=USER_ID,
        access_token="synthetic-user-token",
    )
    application.dependency_overrides[get_supabase_client] = lambda: CLIENT
    application.dependency_overrides[get_market_list_service] = lambda: (
        market_service or FakeMarketListService()
    )
    application.dependency_overrides[get_price_service] = lambda: (
        price_service or FakePriceService()
    )
    application.dependency_overrides[get_watchlist_service] = lambda: WatchlistService(
        repository=repository  # type: ignore[arg-type]
    )
    return application


def assert_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json()["error"] == {
        "code": code,
        "message": response.json()["error"]["message"],
        "details": None,
    }


def test_get_watchlist_requires_auth_before_db_market_or_price() -> None:
    repository = FakeListRepository([])
    market_service = FakeMarketListService(error=AssertionError("must not call"))
    price_service = FakePriceService(error=AssertionError("must not call"))
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    application.dependency_overrides[get_market_list_service] = lambda: market_service
    application.dependency_overrides[get_price_service] = lambda: price_service
    application.dependency_overrides[get_watchlist_service] = lambda: WatchlistService(
        repository=repository  # type: ignore[arg-type]
    )

    with TestClient(application) as client:
        response = client.get("/api/watchlist")

    assert_error(response, 401, "AUTH_REQUIRED")
    assert response.headers["www-authenticate"] == "Bearer"
    assert repository.calls == []
    assert market_service.calls == 0
    assert price_service.calls == []


def test_expired_auth_stops_before_all_business_services() -> None:
    repository = FakeListRepository([])
    market_service = FakeMarketListService()
    price_service = FakePriceService()
    application = query_app(
        repository,
        market_service=market_service,
        price_service=price_service,
    )

    def expired() -> AuthContext:
        raise AuthenticationError.expired()

    application.dependency_overrides[get_auth_context] = expired
    with TestClient(application) as client:
        response = client.get("/api/watchlist")

    assert_error(response, 401, "AUTH_TOKEN_EXPIRED")
    assert repository.calls == []
    assert market_service.calls == 0
    assert price_service.calls == []


def test_empty_watchlist_is_200_and_skips_market_and_price() -> None:
    repository = FakeListRepository([])
    market_service = FakeMarketListService(error=AssertionError("must not call"))
    price_service = FakePriceService(error=AssertionError("must not call"))

    with TestClient(
        query_app(
            repository,
            market_service=market_service,
            price_service=price_service,
        )
    ) as client:
        response = client.get("/api/watchlist")

    assert response.status_code == 200
    assert response.json() == {"data": [], "meta": {"count": 0}}
    assert repository.calls == [{"client": CLIENT, "user_id": USER_ID}]
    assert market_service.calls == 0
    assert price_service.calls == []


def test_success_response_has_exact_contract_fields_numbers_and_db_order() -> None:
    rows = [
        row(1, "KRW-BTC", korean_name="저장 BTC", english_name="Stored BTC"),
        row(2, "KRW-OLD", korean_name="저장 OLD", english_name="Stored OLD"),
        row(3, "KRW-ETH", korean_name="저장 ETH", english_name="Stored ETH"),
        row(4, "KRW-XRP", korean_name="저장 XRP", english_name="Stored XRP"),
    ]
    market_service = FakeMarketListService(
        (
            market("KRW-BTC"),
            market("KRW-ETH", MarketStatus.CAUTION),
            market("KRW-XRP"),
        )
    )
    price_service = FakePriceService(
        {
            "KRW-BTC": price("KRW-BTC", 142_300_000, 0.0125),
            "KRW-ETH": price("KRW-ETH", 4_321_000.25, -0.005, stale=True),
        }
    )

    with TestClient(
        query_app(
            FakeListRepository(rows),
            market_service=market_service,
            price_service=price_service,
        )
    ) as client:
        response = client.get(
            "/api/watchlist",
            params={"userId": str(OTHER_USER_ID), "status": "ACTIVE"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                "id": 1,
                "marketCode": "KRW-BTC",
                "koreanName": "저장 BTC",
                "englishName": "Stored BTC",
                "symbol": "BTC",
                "currentPrice": 142300000,
                "signedChangeRate": 1.25,
                "status": "ACTIVE",
                "isStale": False,
                "createdAt": BASE_TIME.isoformat().replace("+00:00", "Z"),
            },
            {
                "id": 2,
                "marketCode": "KRW-OLD",
                "koreanName": "저장 OLD",
                "englishName": "Stored OLD",
                "symbol": "OLD",
                "currentPrice": None,
                "signedChangeRate": None,
                "status": "UNAVAILABLE",
                "isStale": False,
                "createdAt": BASE_TIME.isoformat().replace("+00:00", "Z"),
            },
            {
                "id": 3,
                "marketCode": "KRW-ETH",
                "koreanName": "저장 ETH",
                "englishName": "Stored ETH",
                "symbol": "ETH",
                "currentPrice": 4321000.25,
                "signedChangeRate": -0.5,
                "status": "CAUTION",
                "isStale": True,
                "createdAt": BASE_TIME.isoformat().replace("+00:00", "Z"),
            },
            {
                "id": 4,
                "marketCode": "KRW-XRP",
                "koreanName": "저장 XRP",
                "englishName": "Stored XRP",
                "symbol": "XRP",
                "currentPrice": None,
                "signedChangeRate": None,
                "status": "PRICE_ERROR",
                "isStale": False,
                "createdAt": BASE_TIME.isoformat().replace("+00:00", "Z"),
            },
        ],
        "meta": {"count": 4},
    }
    assert all(
        set(item)
        == {
            "id",
            "marketCode",
            "koreanName",
            "englishName",
            "symbol",
            "currentPrice",
            "signedChangeRate",
            "status",
            "isStale",
            "createdAt",
        }
        for item in response.json()["data"]
    )
    assert "userId" not in response.text
    assert "market_warning" not in response.text
    assert isinstance(response.json()["data"][0]["currentPrice"], int)
    assert isinstance(response.json()["data"][0]["signedChangeRate"], float)
    assert market_service.calls == 1
    assert price_service.calls == [("KRW-BTC", "KRW-ETH", "KRW-XRP")]


@pytest.mark.parametrize(
    ("market_error", "price_error", "status", "code"),
    [
        (
            AppError(
                code=ErrorCode.CACHE_REFRESH_IN_PROGRESS,
                message="safe cache error",
            ),
            None,
            503,
            "CACHE_REFRESH_IN_PROGRESS",
        ),
        (
            None,
            AppError(
                code=ErrorCode.UPBIT_UNAVAILABLE,
                message="safe ticker error",
            ),
            502,
            "UPBIT_UNAVAILABLE",
        ),
    ],
)
def test_infrastructure_errors_are_not_converted_to_item_statuses(
    market_error: AppError | None,
    price_error: AppError | None,
    status: int,
    code: str,
) -> None:
    market_service = FakeMarketListService(
        (market("KRW-BTC"),),
        error=market_error,
    )
    price_service = FakePriceService(error=price_error)
    repository = FakeListRepository(
        [row(1, "KRW-BTC", korean_name="비트코인", english_name="Bitcoin")]
    )

    with TestClient(
        query_app(
            repository,
            market_service=market_service,
            price_service=price_service,
        )
    ) as client:
        response = client.get("/api/watchlist")

    assert_error(response, status, code)
    assert response.json().get("data") is None


def test_database_failure_is_500_not_empty_success() -> None:
    repository = FakeListRepository(
        [],
        error=WatchlistRepositoryError("private database response"),
    )

    with TestClient(query_app(repository)) as client:
        response = client.get("/api/watchlist")

    assert_error(response, 500, "INTERNAL_SERVER_ERROR")
    assert "private" not in response.text
    assert response.json().get("data") is None
