from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

REMOVED_PATHS = {
    "/api/watchlist",
    "/api/watchlist/{id}",
    "/api/paper/transactions",
}


def test_removed_endpoints_are_absent_from_runtime_and_openapi() -> None:
    app = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    runtime_paths = {route.path for route in app.routes}
    schema_paths = set(app.openapi()["paths"])

    assert REMOVED_PATHS.isdisjoint(runtime_paths)
    assert REMOVED_PATHS.isdisjoint(schema_paths)


def test_removed_endpoint_requests_return_not_found() -> None:
    app = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    with TestClient(app) as client:
        responses = [
            client.get("/api/watchlist"),
            client.post("/api/watchlist", json={}),
            client.delete("/api/watchlist/1"),
            client.get("/api/paper/transactions"),
        ]

    assert all(response.status_code == 404 for response in responses)
