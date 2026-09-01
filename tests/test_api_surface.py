from fastapi.routing import APIRoute

from app.core.config import Settings
from app.main import create_app

COIN_OPERATIONS = {
    ("GET", "/api/coins/search"),
    ("GET", "/api/coins/{marketCode}/chart"),
}
PAPER_OPERATIONS = {
    ("GET", "/api/paper/account"),
    ("POST", "/api/paper/top-ups"),
    ("POST", "/api/paper/trades"),
    ("GET", "/api/paper/portfolio"),
    ("GET", "/api/paper/trades"),
}


def test_final_api_surface_has_exact_coin_and_paper_operations() -> None:
    app = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    operations = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if route.path.startswith(("/api/coins", "/api/paper"))
    }
    coin_operations = {item for item in operations if item[1].startswith("/api/coins")}
    paper_operations = {item for item in operations if item[1].startswith("/api/paper")}

    assert coin_operations == COIN_OPERATIONS
    assert paper_operations == PAPER_OPERATIONS


def test_openapi_documents_exactly_two_coin_and_five_paper_operations() -> None:
    app = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    documented = {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        for method in item
    }
    coin_operations = {item for item in documented if item[1].startswith("/api/coins")}
    paper_operations = {item for item in documented if item[1].startswith("/api/paper")}

    assert coin_operations == COIN_OPERATIONS
    assert paper_operations == PAPER_OPERATIONS
    assert len(coin_operations) == 2
    assert len(paper_operations) == 5
    assert documented - COIN_OPERATIONS - PAPER_OPERATIONS == {("GET", "/api/health")}
