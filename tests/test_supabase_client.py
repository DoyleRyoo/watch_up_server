from collections.abc import Iterator
from typing import Annotated
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from supabase.client import ClientOptions

from app.api.dependencies.auth import get_supabase_http_client
from app.clients.supabase import (
    SUPABASE_DATA_API_TIMEOUT_SECONDS,
    SupabaseConfigurationError,
    create_user_supabase_client,
)
from app.core.config import Settings
from app.main import create_app
from app.models.auth import AuthContext


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        supabase_url="https://project.example.com",
        supabase_anon_key="public-anon-key",
    )


def _context(token: str) -> AuthContext:
    return AuthContext(user_id=uuid4(), access_token=token)


@pytest.fixture
def http_client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=SUPABASE_DATA_API_TIMEOUT_SECONDS) as client:
        yield client


def test_factory_uses_anon_key_and_verified_token_via_public_options(
    monkeypatch: pytest.MonkeyPatch,
    http_client: httpx.Client,
) -> None:
    captured: dict[str, object] = {}
    expected_client = object()

    def fake_create_client(
        url: str,
        key: str,
        options: ClientOptions,
    ) -> object:
        captured.update(url=url, key=key, options=options)
        return expected_client

    monkeypatch.setattr(
        "app.clients.supabase.create_client",
        fake_create_client,
    )
    client = create_user_supabase_client(
        settings=_settings(),
        auth_context=_context("verified-user-token"),
        http_client=http_client,
    )

    assert client is expected_client
    assert captured["url"] == "https://project.example.com"
    assert captured["key"] == "public-anon-key"
    options = captured["options"]
    assert isinstance(options, ClientOptions)
    assert options.headers == {
        "Authorization": "Bearer verified-user-token",
    }
    assert options.auto_refresh_token is False
    assert options.persist_session is False
    assert options.httpx_client is http_client


def test_factory_creates_isolated_clients_on_one_shared_transport() -> None:
    seen_authorization: list[str | None] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("authorization"))
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handle_request)
    with httpx.Client(
        transport=transport,
        timeout=SUPABASE_DATA_API_TIMEOUT_SECONDS,
    ) as shared_http_client:
        client_a = create_user_supabase_client(
            settings=_settings(),
            auth_context=_context("user-a-token"),
            http_client=shared_http_client,
        )
        client_b = create_user_supabase_client(
            settings=_settings(),
            auth_context=_context("user-b-token"),
            http_client=shared_http_client,
        )
        client_a.table("watchlist").select("*").execute()
        client_b.table("watchlist").select("*").execute()

    assert client_a is not client_b
    assert seen_authorization == ["Bearer user-a-token", "Bearer user-b-token"]
    assert client_a.postgrest.headers["apikey"] == "public-anon-key"
    assert client_b.postgrest.headers["apikey"] == "public-anon-key"


def test_factory_does_not_require_service_role_configuration(
    http_client: httpx.Client,
) -> None:
    settings = _settings()

    assert "supabase_service_role_key" not in type(settings).model_fields
    client = create_user_supabase_client(
        settings=settings,
        auth_context=_context("user-token"),
        http_client=http_client,
    )

    assert client.postgrest.headers["apikey"] == "public-anon-key"


def test_missing_data_api_settings_raise_server_configuration_error(
    http_client: httpx.Client,
) -> None:
    with pytest.raises(SupabaseConfigurationError, match="settings are incomplete"):
        create_user_supabase_client(
            settings=Settings(_env_file=None),
            auth_context=_context("user-token"),
            http_client=http_client,
        )


def test_auth_context_repr_never_contains_access_token() -> None:
    context = _context("token-must-stay-private")

    assert "token-must-stay-private" not in repr(context)
    assert str(context.user_id) in repr(context)


def test_shared_http_transport_is_lazy_reused_and_closed_by_lifespan() -> None:
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    seen_clients: list[httpx.Client] = []

    @application.get("/test-transport")
    def test_transport(
        client: Annotated[httpx.Client, Depends(get_supabase_http_client)],
    ) -> dict[str, bool]:
        seen_clients.append(client)
        return {"closed": client.is_closed}

    assert application.state.supabase_http_client is None
    with TestClient(application) as client:
        first = client.get("/test-transport")
        second = client.get("/test-transport")
        shared_client = application.state.supabase_http_client
        assert first.json() == second.json() == {"closed": False}
        assert seen_clients == [shared_client, shared_client]
        assert shared_client.is_closed is False

    assert shared_client.is_closed is True
