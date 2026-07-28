from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_auth_context, get_supabase_client
from app.api.dependencies.services import (
    get_market_list_service,
    get_watchlist_service,
)
from app.core.config import Settings
from app.core.errors import AppError, ErrorCode
from app.core.exceptions import AuthenticationError
from app.main import create_app
from app.models.auth import AuthContext
from app.repositories.watchlist import WatchlistDuplicateError
from app.services.watchlist import WatchlistService
from tests.test_watchlist_registration_service import (
    CLIENT,
    CREATED_AT,
    USER_ID,
    FakeMarketListService,
    RecordingRepository,
)


OTHER_USER_ID = uuid4()


def registration_app(
    repository: RecordingRepository,
    *,
    market_service: FakeMarketListService | None = None,
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
    application.dependency_overrides[get_watchlist_service] = lambda: WatchlistService(
        repository=repository  # type: ignore[arg-type]
    )
    return application


def client_for(application: FastAPI) -> Iterator[TestClient]:
    with TestClient(application) as client:
        yield client


def assert_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["details"] is None
    assert set(response.json()) == {"error"}


def test_registration_requires_auth_before_any_service_work() -> None:
    repository = RecordingRepository()
    market_service = FakeMarketListService()
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    application.dependency_overrides[get_market_list_service] = lambda: market_service
    application.dependency_overrides[get_watchlist_service] = lambda: WatchlistService(
        repository=repository  # type: ignore[arg-type]
    )

    with TestClient(application) as client:
        response = client.post("/api/watchlist", content=b"{")

    assert_error(response, 401, "AUTH_REQUIRED")
    assert response.headers["www-authenticate"] == "Bearer"
    assert market_service.calls == []
    assert repository.calls == []


def test_expired_auth_stops_before_registration() -> None:
    repository = RecordingRepository()
    market_service = FakeMarketListService()
    application = registration_app(repository, market_service=market_service)

    def expired() -> AuthContext:
        raise AuthenticationError.expired()

    application.dependency_overrides[get_auth_context] = expired
    with TestClient(application) as client:
        response = client.post(
            "/api/watchlist",
            json={"marketCode": "KRW-BTC"},
        )

    assert_error(response, 401, "AUTH_TOKEN_EXPIRED")
    assert market_service.calls == []
    assert repository.calls == []


@pytest.mark.parametrize(
    ("request_kwargs"),
    [
        {},
        {"content": b"{"},
        {"json": {}},
        {"json": {"marketCode": None}},
        {"json": {"marketCode": 123}},
        {"json": {"marketCode": ["KRW-BTC"]}},
        {"json": []},
    ],
)
def test_invalid_body_uses_common_400_envelope(
    request_kwargs: dict[str, object],
) -> None:
    repository = RecordingRepository()
    market_service = FakeMarketListService()

    with TestClient(
        registration_app(repository, market_service=market_service)
    ) as client:
        response = client.post("/api/watchlist", **request_kwargs)

    assert response.json() == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "요청값이 올바르지 않습니다.",
            "details": None,
        }
    }
    assert response.status_code == 400
    assert market_service.calls == []
    assert repository.calls == []
    assert response.status_code != 422


@pytest.mark.parametrize(
    "market_code",
    [
        "",
        "   ",
        "BTC",
        "USD-BTC",
        "krw-btc",
        "KRW-",
        "KRW-BTC ",
        "KRW-B_TC",
        "KRW-ABCDEFGHIJKLMNOPQ",
    ],
)
def test_invalid_market_code_is_400_without_external_or_database_calls(
    market_code: str,
) -> None:
    repository = RecordingRepository()
    market_service = FakeMarketListService()

    with TestClient(
        registration_app(repository, market_service=market_service)
    ) as client:
        response = client.post("/api/watchlist", json={"marketCode": market_code})

    assert_error(response, 400, "INVALID_MARKET_CODE")
    assert market_service.calls == []
    assert repository.calls == []


def test_unknown_market_is_invalid_but_market_infrastructure_error_is_preserved() -> (
    None
):
    missing_repository = RecordingRepository()
    missing_market = FakeMarketListService(result=None)
    with TestClient(
        registration_app(missing_repository, market_service=missing_market)
    ) as client:
        missing = client.post(
            "/api/watchlist",
            json={"marketCode": "KRW-BTC"},
        )

    assert_error(missing, 400, "INVALID_MARKET_CODE")
    assert missing_repository.calls == []

    outage_repository = RecordingRepository()
    outage_market = FakeMarketListService(
        error=AppError(
            code=ErrorCode.CACHE_REFRESH_IN_PROGRESS,
            message="마켓 목록을 갱신하고 있습니다.",
        )
    )
    with TestClient(
        registration_app(outage_repository, market_service=outage_market)
    ) as client:
        outage = client.post(
            "/api/watchlist",
            json={"marketCode": "KRW-BTC"},
        )

    assert_error(outage, 503, "CACHE_REFRESH_IN_PROGRESS")
    assert outage_repository.calls == []


def test_success_uses_verified_user_server_names_and_database_values() -> None:
    repository = RecordingRepository()
    payload = {
        "marketCode": "KRW-BTC",
        "userId": str(OTHER_USER_ID),
        "koreanName": "조작된 이름",
        "englishName": "Fake Name",
        "id": 999,
        "createdAt": "2000-01-01T00:00:00Z",
        "status": "CAUTION",
        "currentPrice": 1,
    }

    with TestClient(registration_app(repository)) as client:
        response = client.post("/api/watchlist", json=payload)

    assert response.status_code == 201
    assert response.json() == {
        "data": {
            "id": 37,
            "marketCode": "KRW-BTC",
            "koreanName": "비트코인",
            "englishName": "Bitcoin",
            "createdAt": CREATED_AT.isoformat().replace("+00:00", "Z"),
        },
        "meta": None,
    }
    assert set(response.json()["data"]) == {
        "id",
        "marketCode",
        "koreanName",
        "englishName",
        "createdAt",
    }
    values = repository.calls[-1][1]["values"]
    assert values.user_id == USER_ID  # type: ignore[union-attr]
    assert values.korean_name == "비트코인"  # type: ignore[union-attr]
    assert values.english_name == "Bitcoin"  # type: ignore[union-attr]
    assert OTHER_USER_ID not in repository.calls[-1][1].values()


@pytest.mark.parametrize(
    ("repository", "status", "code"),
    [
        (RecordingRepository(count=50), 400, "WATCHLIST_LIMIT_EXCEEDED"),
        (RecordingRepository(exists=True), 409, "WATCHLIST_DUPLICATED"),
        (
            RecordingRepository(
                insert_error=WatchlistDuplicateError("private unique details")
            ),
            409,
            "WATCHLIST_DUPLICATED",
        ),
    ],
)
def test_limit_and_both_duplicate_paths_use_public_errors(
    repository: RecordingRepository,
    status: int,
    code: str,
) -> None:
    with TestClient(registration_app(repository)) as client:
        response = client.post(
            "/api/watchlist",
            json={"marketCode": "KRW-BTC"},
        )

    assert_error(response, status, code)
    assert "private" not in response.text


def test_openapi_documents_required_camel_case_request_body() -> None:
    operation = registration_app(RecordingRepository()).openapi()["paths"][
        "/api/watchlist"
    ]["post"]
    request_body = operation["requestBody"]
    schema = request_body["content"]["application/json"]["schema"]

    assert request_body["required"] is True
    assert schema["required"] == ["marketCode"]
    assert set(schema["properties"]) == {"marketCode"}


def test_router_registers_only_get_and_post_for_public_watchlist_at_this_stage() -> (
    None
):
    application = registration_app(RecordingRepository())
    watchlist_routes = {
        (method, route.path)
        for route in application.routes
        if route.path.startswith("/api/watchlist")
        for method in route.methods
    }

    assert watchlist_routes == {
        ("GET", "/api/watchlist"),
        ("POST", "/api/watchlist"),
    }
