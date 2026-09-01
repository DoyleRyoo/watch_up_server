import logging
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import ERROR_STATUS_CODES, AppError, ErrorCode
from app.main import create_app
from app.schemas.base import APIModel
from app.schemas.common import (
    ErrorContent,
    ErrorResponse,
    ListMeta,
    ListResponse,
    SuccessResponse,
)


class SampleData(APIModel):
    market_code: str
    korean_name: str
    created_at: datetime


class ValidationBody(APIModel):
    market_code: str


SAMPLE = SampleData(
    market_code="KRW-BTC",
    korean_name="비트코인",
    created_at=datetime(2026, 7, 25, tzinfo=UTC),
)


EXPECTED_STATUS_CODES = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.INVALID_MARKET_CODE: 400,
    ErrorCode.WATCHLIST_LIMIT_EXCEEDED: 400,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.AUTH_TOKEN_EXPIRED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.WATCHLIST_NOT_FOUND: 404,
    ErrorCode.WATCHLIST_DUPLICATED: 409,
    ErrorCode.UPBIT_UNAVAILABLE: 502,
    ErrorCode.UPBIT_RATE_LIMITED: 503,
    ErrorCode.UPBIT_TEMPORARILY_BLOCKED: 503,
    ErrorCode.REDIS_UNAVAILABLE: 503,
    ErrorCode.CACHE_REFRESH_IN_PROGRESS: 503,
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED: 400,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: 409,
    ErrorCode.TOP_UP_AMOUNT_OUT_OF_RANGE: 400,
    ErrorCode.TOP_UP_LIFETIME_LIMIT_EXCEEDED: 400,
    ErrorCode.DATABASE_UNAVAILABLE: 503,
    ErrorCode.MARKET_NOT_TRADABLE: 400,
    ErrorCode.INSUFFICIENT_CASH_BALANCE: 400,
    ErrorCode.INSUFFICIENT_HOLDING_QUANTITY: 400,
    ErrorCode.INTERNAL_SERVER_ERROR: 500,
}


def _temporary_app() -> FastAPI:
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)

    @application.get("/test-single", response_model=SuccessResponse[SampleData])
    def single_response() -> SuccessResponse[SampleData]:
        return SuccessResponse(data=SAMPLE)

    @application.get("/test-list", response_model=ListResponse[SampleData])
    def list_response() -> ListResponse[SampleData]:
        return ListResponse(data=[SAMPLE], meta=ListMeta(count=1))

    @application.get("/test-app-error")
    def app_error() -> None:
        raise AppError(
            code=ErrorCode.WATCHLIST_DUPLICATED,
            message="이미 등록된 코인입니다.",
        )

    @application.get("/test-query")
    def query_validation(value: int) -> dict[str, int]:
        return {"value": value}

    @application.get("/test-path/{item_id}")
    def path_validation(item_id: int) -> dict[str, int]:
        return {"itemId": item_id}

    @application.post("/test-body")
    def body_validation(payload: ValidationBody) -> ValidationBody:
        return payload

    return application


def _assert_invalid_request(response: Any) -> None:
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "요청값이 올바르지 않습니다.",
            "details": None,
        }
    }
    assert "detail" not in response.json()
    assert "data" not in response.json()
    assert "meta" not in response.json()


def test_single_success_schema_serializes_camel_case() -> None:
    response = SuccessResponse(data=SAMPLE)

    assert response.model_dump(mode="json") == {
        "data": {
            "marketCode": "KRW-BTC",
            "koreanName": "비트코인",
            "createdAt": "2026-07-25T00:00:00Z",
        },
        "meta": None,
    }
    assert "error" not in response.model_dump(mode="json")


def test_single_success_http_response_uses_aliases() -> None:
    with TestClient(_temporary_app()) as client:
        response = client.get("/test-single")

    assert response.status_code == 200
    assert response.json() == {
        "data": {
            "marketCode": "KRW-BTC",
            "koreanName": "비트코인",
            "createdAt": "2026-07-25T00:00:00Z",
        },
        "meta": None,
    }
    assert "market_code" not in response.text
    assert "korean_name" not in response.text
    assert "created_at" not in response.text


def test_list_schema_count_matches_data() -> None:
    response = ListResponse(data=[SAMPLE], meta=ListMeta(count=1))

    assert response.model_dump(mode="json") == {
        "data": [
            {
                "marketCode": "KRW-BTC",
                "koreanName": "비트코인",
                "createdAt": "2026-07-25T00:00:00Z",
            }
        ],
        "meta": {"count": 1},
    }


def test_list_schema_can_derive_count_and_rejects_mismatch() -> None:
    derived = ListResponse[SampleData].model_validate({"data": [SAMPLE]})
    assert derived.meta.count == 1

    with pytest.raises(ValidationError):
        ListResponse(data=[SAMPLE], meta=ListMeta(count=0))

    with pytest.raises(ValidationError):
        ListMeta(count=-1)


def test_list_http_response_has_no_error_member() -> None:
    with TestClient(_temporary_app()) as client:
        response = client.get("/test-list")

    assert response.status_code == 200
    assert response.json()["meta"] == {"count": len(response.json()["data"])}
    assert "error" not in response.json()


def test_error_schema_defaults_details_to_null_and_has_no_success_members() -> None:
    response = ErrorResponse(
        error=ErrorContent(
            code=ErrorCode.INVALID_REQUEST,
            message="요청값이 올바르지 않습니다.",
        )
    )

    assert response.model_dump(mode="json") == {
        "error": {
            "code": "INVALID_REQUEST",
            "message": "요청값이 올바르지 않습니다.",
            "details": None,
        }
    }
    assert "data" not in response.model_dump(mode="json")
    assert "meta" not in response.model_dump(mode="json")


def test_every_documented_error_code_has_one_immutable_status() -> None:
    assert set(ErrorCode) == set(EXPECTED_STATUS_CODES)
    assert dict(ERROR_STATUS_CODES) == EXPECTED_STATUS_CODES

    mapping = cast(dict[ErrorCode, int], ERROR_STATUS_CODES)
    with pytest.raises(TypeError):
        mapping[ErrorCode.INVALID_REQUEST] = 418


def test_app_error_uses_mapped_status_and_common_envelope() -> None:
    with TestClient(_temporary_app()) as client:
        response = client.get("/test-app-error")

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {
            "code": "WATCHLIST_DUPLICATED",
            "message": "이미 등록된 코인입니다.",
            "details": None,
        }
    }


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/test-query", {}),
        ("get", "/test-query?value=not-an-integer", {}),
        ("get", "/test-path/not-an-integer", {}),
        ("post", "/test-body", {"json": {}}),
        ("post", "/test-body", {"json": {"marketCode": 123}}),
        (
            "post",
            "/test-body",
            {"content": "{", "headers": {"Content-Type": "application/json"}},
        ),
    ],
)
def test_request_validation_is_common_400_error(
    method: str,
    path: str,
    kwargs: dict[str, Any],
) -> None:
    with TestClient(_temporary_app()) as client:
        response = client.request(method, path, **kwargs)

    _assert_invalid_request(response)
    assert "not-an-integer" not in response.text


def test_camel_case_names_appear_in_json_schema() -> None:
    schema = SampleData.model_json_schema(mode="serialization")

    assert set(schema["properties"]) == {"marketCode", "koreanName", "createdAt"}


def test_health_openapi_uses_common_response_model(client: TestClient) -> None:
    openapi = client.get("/openapi.json").json()
    schema = openapi["paths"]["/api/health"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    component_name = schema["$ref"].rsplit("/", 1)[1]
    response_component = openapi["components"]["schemas"][component_name]

    assert set(response_component["properties"]) == {"data", "meta"}


def test_production_router_contains_no_test_endpoint(test_app: FastAPI) -> None:
    api_paths = {
        route.path for route in test_app.routes if route.path.startswith("/api")
    }

    assert api_paths == {
        "/api/health",
        "/api/coins/search",
        "/api/coins/{marketCode}/chart",
        "/api/paper/account",
        "/api/paper/top-ups",
        "/api/paper/trades",
    }


def test_undefined_routes_and_methods_keep_framework_behavior() -> None:
    with TestClient(_temporary_app()) as client:
        not_found = client.get("/does-not-exist")
        method_not_allowed = client.post("/test-query")

    assert not_found.status_code == 404
    assert not_found.json() == {"detail": "Not Found"}
    assert method_not_allowed.status_code == 405
    assert method_not_allowed.json() == {"detail": "Method Not Allowed"}


def test_unexpected_error_does_not_log_or_return_authorization(
    caplog: pytest.LogCaptureFixture,
) -> None:
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    sensitive_token = "request-token-must-not-leak"

    @application.get("/test-unexpected")
    def unexpected_error() -> None:
        raise RuntimeError("safe diagnostic message")

    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.get(
                "/test-unexpected",
                headers={"Authorization": f"Bearer {sensitive_token}"},
            )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert sensitive_token not in response.text
    assert sensitive_token not in caplog.text
