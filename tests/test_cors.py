from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import ALLOWED_CORS_METHODS, create_app


def test_allowed_origin_preflight_uses_restricted_policy() -> None:
    settings = Settings(
        _env_file=None,
        cors_allowed_origins="http://localhost:5173, https://watchup.example.com",
    )
    with TestClient(create_app(settings, load_markets_on_startup=False)) as client:
        response = client.options(
            "/api/health",
            headers={
                "Origin": "https://watchup.example.com",
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "Authorization, Content-Type",
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
    assert returned_methods == set(ALLOWED_CORS_METHODS)
    assert "access-control-allow-credentials" not in response.headers


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

    assert "access-control-allow-origin" not in response.headers
