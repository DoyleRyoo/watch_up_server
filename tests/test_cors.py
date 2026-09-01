from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import ALLOWED_CORS_HEADERS, ALLOWED_CORS_METHODS, create_app


def test_allowed_origin_preflight_uses_final_policy() -> None:
    settings = Settings(
        _env_file=None,
        cors_allowed_origins="http://localhost:5173, https://watchup.example.com",
    )
    with TestClient(create_app(settings, load_markets_on_startup=False)) as client:
        response = client.options(
            "/api/paper/trades",
            headers={
                "Origin": "https://watchup.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "Authorization, Content-Type, Idempotency-Key"
                ),
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "https://watchup.example.com"
    )
    returned_methods = {
        method.strip()
        for method in response.headers["access-control-allow-methods"].split(",")
    }
    returned_headers = response.headers["access-control-allow-headers"].casefold()
    assert returned_methods == {"GET", "POST", "OPTIONS"}
    assert ALLOWED_CORS_METHODS == ["GET", "POST", "OPTIONS"]
    assert ALLOWED_CORS_HEADERS == ["Authorization", "Content-Type", "Idempotency-Key"]
    assert "idempotency-key" in returned_headers
    assert "access-control-allow-credentials" not in response.headers


def test_delete_preflight_is_rejected() -> None:
    settings = Settings(
        _env_file=None,
        cors_allowed_origins="https://watchup.example.com",
    )
    with TestClient(create_app(settings, load_markets_on_startup=False)) as client:
        response = client.options(
            "/api/paper/trades",
            headers={
                "Origin": "https://watchup.example.com",
                "Access-Control-Request-Method": "DELETE",
            },
        )

    assert response.status_code == 400
    assert "DELETE" not in response.headers["access-control-allow-methods"]


def test_disallowed_origin_does_not_receive_allow_origin_header() -> None:
    settings = Settings(
        _env_file=None,
        cors_allowed_origins="http://localhost:5173",
    )
    with TestClient(create_app(settings, load_markets_on_startup=False)) as client:
        response = client.options(
            "/api/health",
            headers={
                "Origin": "https://untrusted.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
