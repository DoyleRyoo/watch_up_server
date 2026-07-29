from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_auth_context, get_supabase_client
from app.api.dependencies.services import (
    get_chart_service,
    get_market_list_service,
    get_price_service,
    get_watchlist_service,
)
from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.main import create_app
from app.models.auth import AuthContext
from app.models.watchlist import POSTGRES_BIGINT_MAX
from app.repositories.watchlist import (
    WatchlistNotFoundError,
    WatchlistRepositoryError,
)
from app.services.watchlist import WatchlistService


USER_ID = uuid4()
OTHER_USER_ID = uuid4()
CLIENT = object()


class RecordingDeleteRepository:
    def __init__(
        self,
        *,
        result: int = 1,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, object]] = []

    def delete_by_user_and_id(self, **arguments: object) -> int:
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        return self.result


def delete_app(repository: RecordingDeleteRepository) -> FastAPI:
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    application.dependency_overrides[get_auth_context] = lambda: AuthContext(
        user_id=USER_ID,
        access_token="synthetic-user-token",
    )
    application.dependency_overrides[get_supabase_client] = lambda: CLIENT
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
    assert set(response.json()) == {"error"}


def test_delete_requires_auth_before_database_or_other_services() -> None:
    repository = RecordingDeleteRepository()
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    application.dependency_overrides[get_watchlist_service] = lambda: WatchlistService(
        repository=repository  # type: ignore[arg-type]
    )

    with TestClient(application) as client:
        response = client.delete("/api/watchlist/1")

    assert_error(response, 401, "AUTH_REQUIRED")
    assert response.headers["www-authenticate"] == "Bearer"
    assert repository.calls == []
    assert application.state.supabase_http_client is None


def test_expired_auth_stops_before_delete() -> None:
    repository = RecordingDeleteRepository()
    application = delete_app(repository)

    def expired() -> AuthContext:
        raise AuthenticationError.expired()

    application.dependency_overrides[get_auth_context] = expired
    with TestClient(application) as client:
        response = client.delete("/api/watchlist/1")

    assert_error(response, 401, "AUTH_TOKEN_EXPIRED")
    assert repository.calls == []


@pytest.mark.parametrize("watchlist_id", [1, POSTGRES_BIGINT_MAX])
def test_delete_success_uses_verified_user_scoped_client_and_exact_contract(
    watchlist_id: int,
) -> None:
    repository = RecordingDeleteRepository(result=watchlist_id)
    application = delete_app(repository)
    forbidden_calls: list[str] = []

    def forbidden_service(name: str) -> None:
        forbidden_calls.append(name)
        raise AssertionError(f"{name} must not be resolved for delete")

    application.dependency_overrides[get_market_list_service] = lambda: (
        forbidden_service("market")
    )
    application.dependency_overrides[get_price_service] = lambda: forbidden_service(
        "price"
    )
    application.dependency_overrides[get_chart_service] = lambda: forbidden_service(
        "chart"
    )

    with TestClient(application) as client:
        response = client.request(
            "DELETE",
            f"/api/watchlist/{watchlist_id}",
            params={"userId": str(OTHER_USER_ID)},
            json={
                "id": 999,
                "userId": str(OTHER_USER_ID),
                "marketCode": "KRW-BTC",
            },
        )

    assert response.status_code == 200
    assert response.status_code != 204
    assert response.json() == {"data": {"id": watchlist_id}, "meta": None}
    assert set(response.json()["data"]) == {"id"}
    assert "userId" not in response.text
    assert "marketCode" not in response.text
    assert repository.calls == [
        {
            "client": CLIENT,
            "user_id": USER_ID,
            "watchlist_id": watchlist_id,
        }
    ]
    assert OTHER_USER_ID not in repository.calls[0].values()
    assert forbidden_calls == []


@pytest.mark.parametrize(
    "path_value",
    [
        "not-an-integer",
        "1.0",
        "0",
        "-1",
        str(POSTGRES_BIGINT_MAX + 1),
    ],
)
def test_invalid_delete_id_uses_common_400_without_database_call(
    path_value: str,
) -> None:
    repository = RecordingDeleteRepository()

    with TestClient(delete_app(repository)) as client:
        response = client.delete(f"/api/watchlist/{path_value}")

    assert response.json() == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "요청값이 올바르지 않습니다.",
            "details": None,
        }
    }
    assert response.status_code == 400
    assert response.status_code != 422
    assert repository.calls == []


def test_empty_delete_path_does_not_fabricate_a_delete_success() -> None:
    repository = RecordingDeleteRepository()

    with TestClient(delete_app(repository), follow_redirects=False) as client:
        response = client.delete("/api/watchlist/")

    assert response.status_code in {307, 405}
    assert repository.calls == []


def test_missing_or_rls_invisible_delete_target_is_same_safe_404() -> None:
    repository = RecordingDeleteRepository(
        error=WatchlistNotFoundError("private row visibility detail")
    )

    with TestClient(delete_app(repository)) as client:
        response = client.delete("/api/watchlist/31")

    assert_error(response, 404, "WATCHLIST_NOT_FOUND")
    assert "private" not in response.text
    assert repository.calls == [
        {
            "client": CLIENT,
            "user_id": USER_ID,
            "watchlist_id": 31,
        }
    ]


def test_delete_database_error_is_safe_500_not_success_or_not_found() -> None:
    repository = RecordingDeleteRepository(
        error=WatchlistRepositoryError("private-token https://database.example.invalid")
    )

    with TestClient(delete_app(repository)) as client:
        response = client.delete("/api/watchlist/31")

    assert_error(response, 500, "INTERNAL_SERVER_ERROR")
    assert "private-token" not in response.text
    assert "database.example.invalid" not in response.text
    assert response.json().get("data") is None


def test_delete_openapi_uses_bigint_path_and_common_success_model() -> None:
    application = delete_app(RecordingDeleteRepository())
    operation = application.openapi()["paths"]["/api/watchlist/{id}"]["delete"]
    path_parameter = next(
        parameter for parameter in operation["parameters"] if parameter["in"] == "path"
    )
    response_schema = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]

    assert path_parameter["name"] == "id"
    assert path_parameter["required"] is True
    assert path_parameter["schema"]["minimum"] == 1
    assert path_parameter["schema"]["maximum"] == POSTGRES_BIGINT_MAX
    assert response_schema["$ref"].endswith("SuccessResponse_WatchlistDeletedItem_")
