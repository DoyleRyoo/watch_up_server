from fastapi.testclient import TestClient


def test_health_is_public_and_returns_expected_body(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"data": {"status": "ok"}, "meta": None}
