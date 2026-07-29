from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_unexpected_exception_uses_common_error_envelope() -> None:
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)

    @application.get("/test-error")
    async def raise_unexpected_error() -> None:
        raise RuntimeError("internal detail must not be returned")

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/test-error")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_SERVER_ERROR",
            "message": "서버 내부 오류가 발생했습니다.",
            "details": None,
        }
    }
    assert "internal detail" not in response.text


def test_http_exception_keeps_fastapi_default_behavior() -> None:
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)

    @application.get("/test-http-error")
    async def raise_http_error() -> None:
        raise HTTPException(status_code=418, detail="expected error")

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/test-http-error")

    assert response.status_code == 418
    assert response.json() == {"detail": "expected error"}
