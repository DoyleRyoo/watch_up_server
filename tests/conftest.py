from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def test_app() -> FastAPI:
    settings = Settings(_env_file=None)
    return create_app(settings, load_markets_on_startup=False)


@pytest.fixture
def client(test_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(test_app) as test_client:
        yield test_client
