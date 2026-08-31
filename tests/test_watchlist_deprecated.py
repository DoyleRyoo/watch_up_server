from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_legacy_watchlist_routes_are_not_registered() -> None:
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)

    assert "/api/watchlist" not in application.openapi()["paths"]
    assert "/api/watchlist/{id}" not in application.openapi()["paths"]

    with TestClient(application) as client:
        assert client.get("/api/watchlist").status_code == 404
        assert (
            client.post("/api/watchlist", json={"marketCode": "KRW-BTC"}).status_code
            == 404
        )
        assert client.delete("/api/watchlist/1").status_code == 404
