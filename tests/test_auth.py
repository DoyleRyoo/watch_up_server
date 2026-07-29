import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Annotated, Any
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from app.api.dependencies.auth import get_auth_context
from app.api.dependencies.services import get_market_list_service
from app.core.config import Settings
from app.main import create_app
from app.models.auth import AuthContext
from app.models.market import Market, MarketStatus


ISSUER = "https://project.example.com/auth/v1"
AUDIENCE = "authenticated"
KEY_ID = "test-key-1"


class _JWKSState:
    def __init__(self, jwks: dict[str, Any]) -> None:
        self.jwks = jwks
        self.request_count = 0
        self.lock = Lock()


class _JWKSServer(ThreadingHTTPServer):
    state: _JWKSState


class _JWKSHandler(BaseHTTPRequestHandler):
    server: _JWKSServer

    def do_GET(self) -> None:
        with self.server.state.lock:
            self.server.state.request_count += 1
            payload = json.dumps(self.server.state.jwks).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.fixture
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_jwk(key: rsa.RSAPrivateKey, *, key_id: str) -> dict[str, Any]:
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update({"alg": "RS256", "kid": key_id, "use": "sig"})
    return jwk


@pytest.fixture
def jwks_server(
    signing_key: rsa.RSAPrivateKey,
) -> Iterator[tuple[_JWKSState, str]]:
    state = _JWKSState({"keys": [_public_jwk(signing_key, key_id=KEY_ID)]})
    server = _JWKSServer(("127.0.0.1", 0), _JWKSHandler)
    server.state = state
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield state, f"http://{host}:{port}/.well-known/jwks.json"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _token(
    key: rsa.RSAPrivateKey,
    *,
    key_id: str = KEY_ID,
    subject: str | None = None,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    expires_at: datetime | None = None,
    include_subject: bool = True,
) -> tuple[str, UUID]:
    user_id = UUID(subject) if subject is not None else uuid4()
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "exp": expires_at or datetime.now(UTC) + timedelta(minutes=5),
    }
    if include_subject:
        claims["sub"] = subject if subject is not None else str(user_id)
    encoded = jwt.encode(
        claims,
        key,
        algorithm="RS256",
        headers={"kid": key_id},
    )
    return encoded, user_id


def _protected_app(jwks_url: str) -> FastAPI:
    settings = Settings(
        _env_file=None,
        supabase_jwks_url=jwks_url,
        supabase_issuer=ISSUER,
        supabase_audience=AUDIENCE,
    )
    application = create_app(settings, load_markets_on_startup=False)

    @application.get("/test-auth")
    def test_auth(
        auth_context: Annotated[AuthContext, Depends(get_auth_context)],
    ) -> dict[str, str]:
        return {"userId": str(auth_context.user_id)}

    return application


def _assert_auth_error(response: Any, code: str) -> None:
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {
            "code": code,
            "message": (
                "인증 토큰이 만료되었습니다."
                if code == "AUTH_TOKEN_EXPIRED"
                else "인증이 필요합니다."
            ),
            "details": None,
        }
    }


def test_health_is_lazy_and_does_not_construct_authentication_resources() -> None:
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)
    assert application.state.jwt_verifier is None
    assert application.state.supabase_http_client is None

    with TestClient(application) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert application.state.jwt_verifier is None
    assert application.state.supabase_http_client is None


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic value", "Bearer", "Bearer   ", "Bearer one two", "Bearer\tone"],
)
def test_missing_or_malformed_authorization_is_rejected(
    jwks_server: tuple[_JWKSState, str],
    authorization: str | None,
) -> None:
    _, jwks_url = jwks_server
    headers = {} if authorization is None else {"Authorization": authorization}

    with TestClient(_protected_app(jwks_url)) as client:
        response = client.get("/test-auth", headers=headers)

    _assert_auth_error(response, "AUTH_REQUIRED")


def test_valid_token_returns_verified_subject_and_scheme_is_case_insensitive(
    jwks_server: tuple[_JWKSState, str],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    _, jwks_url = jwks_server
    access_token, user_id = _token(signing_key)

    with TestClient(_protected_app(jwks_url)) as client:
        response = client.get(
            "/test-auth",
            headers={"Authorization": f"bEaReR {access_token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"userId": str(user_id)}


def test_expired_token_has_specific_error(
    jwks_server: tuple[_JWKSState, str],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    _, jwks_url = jwks_server
    access_token, _ = _token(
        signing_key,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    with TestClient(_protected_app(jwks_url)) as client:
        response = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    _assert_auth_error(response, "AUTH_TOKEN_EXPIRED")


@pytest.mark.parametrize("claim", ["signature", "issuer", "audience"])
def test_invalid_signature_issuer_and_audience_are_generic_auth_failures(
    jwks_server: tuple[_JWKSState, str],
    signing_key: rsa.RSAPrivateKey,
    claim: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, jwks_url = jwks_server
    token_key = signing_key
    issuer = ISSUER
    audience = AUDIENCE
    if claim == "signature":
        token_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif claim == "issuer":
        issuer = "https://wrong.example.com/auth/v1"
    else:
        audience = "wrong-audience"
    access_token, _ = _token(
        token_key,
        issuer=issuer,
        audience=audience,
    )

    with TestClient(_protected_app(jwks_url)) as client:
        response = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    _assert_auth_error(response, "AUTH_REQUIRED")
    assert access_token not in response.text
    assert access_token not in caplog.text


@pytest.mark.parametrize("subject", [None, "", "not-a-uuid"])
def test_missing_empty_or_non_uuid_subject_is_rejected(
    jwks_server: tuple[_JWKSState, str],
    signing_key: rsa.RSAPrivateKey,
    subject: str | None,
) -> None:
    _, jwks_url = jwks_server
    if subject is None:
        access_token, _ = _token(signing_key, include_subject=False)
    else:
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "sub": subject,
        }
        access_token = jwt.encode(
            claims,
            signing_key,
            algorithm="RS256",
            headers={"kid": KEY_ID},
        )

    with TestClient(_protected_app(jwks_url)) as client:
        response = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    _assert_auth_error(response, "AUTH_REQUIRED")


def test_missing_kid_is_rejected_without_fetching_jwks(
    jwks_server: tuple[_JWKSState, str],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    state, jwks_url = jwks_server
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "sub": str(uuid4()),
        },
        signing_key,
        algorithm="RS256",
    )

    with TestClient(_protected_app(jwks_url)) as client:
        response = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {token}"},
        )

    _assert_auth_error(response, "AUTH_REQUIRED")
    assert state.request_count == 0


def test_alg_none_is_rejected_without_fetching_jwks(
    jwks_server: tuple[_JWKSState, str],
) -> None:
    state, jwks_url = jwks_server
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
            "sub": str(uuid4()),
        },
        key=None,
        algorithm="none",
        headers={"kid": KEY_ID},
    )

    with TestClient(_protected_app(jwks_url)) as client:
        response = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {token}"},
        )

    _assert_auth_error(response, "AUTH_REQUIRED")
    assert state.request_count == 0


def test_unknown_kid_refreshes_once_then_is_rejected(
    jwks_server: tuple[_JWKSState, str],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    state, jwks_url = jwks_server
    access_token, _ = _token(signing_key, key_id="unknown-key")

    with TestClient(_protected_app(jwks_url)) as client:
        first = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        second = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    _assert_auth_error(first, "AUTH_REQUIRED")
    _assert_auth_error(second, "AUTH_REQUIRED")
    assert state.request_count == 2


def test_repeated_known_kid_uses_jwks_cache(
    jwks_server: tuple[_JWKSState, str],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    state, jwks_url = jwks_server
    access_token, _ = _token(signing_key)

    with TestClient(_protected_app(jwks_url)) as client:
        first = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        second = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert first.status_code == second.status_code == 200
    assert state.request_count == 1


def test_new_kid_forces_one_refresh_and_accepts_rotated_key(
    jwks_server: tuple[_JWKSState, str],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    state, jwks_url = jwks_server
    first_token, _ = _token(signing_key)
    rotated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rotated_token, _ = _token(rotated_key, key_id="test-key-2")

    with TestClient(_protected_app(jwks_url)) as client:
        first = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        state.jwks = {
            "keys": [
                _public_jwk(signing_key, key_id=KEY_ID),
                _public_jwk(rotated_key, key_id="test-key-2"),
            ]
        }
        second = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {rotated_token}"},
        )

    assert first.status_code == second.status_code == 200
    assert state.request_count == 2


def test_invalid_jwks_is_internal_error_not_expired_token(
    jwks_server: tuple[_JWKSState, str],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    state, jwks_url = jwks_server
    state.jwks = {"unexpected": []}
    access_token, _ = _token(signing_key)

    with TestClient(
        _protected_app(jwks_url),
        raise_server_exceptions=False,
    ) as client:
        response = client.get(
            "/test-auth",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert "AUTH_TOKEN_EXPIRED" not in response.text
    assert access_token not in response.text


def test_missing_auth_server_settings_are_internal_error() -> None:
    application = create_app(Settings(_env_file=None), load_markets_on_startup=False)

    @application.get("/test-auth")
    def test_auth(
        auth_context: Annotated[AuthContext, Depends(get_auth_context)],
    ) -> dict[str, str]:
        return {"userId": str(auth_context.user_id)}

    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get(
            "/test-auth",
            headers={"Authorization": "Bearer token-without-settings"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"


def test_coin_search_valid_jwt_reaches_service_without_supabase_client(
    jwks_server: tuple[_JWKSState, str],
    signing_key: rsa.RSAPrivateKey,
) -> None:
    _, jwks_url = jwks_server
    access_token, _ = _token(signing_key)
    settings = Settings(
        _env_file=None,
        supabase_jwks_url=jwks_url,
        supabase_issuer=ISSUER,
        supabase_audience=AUDIENCE,
    )
    application = create_app(settings, load_markets_on_startup=False)

    class SearchServiceStub:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def search(self, query: str) -> tuple[Market, ...]:
            self.calls.append(query)
            return (
                Market(
                    market_code="KRW-BTC",
                    korean_name="비트코인",
                    english_name="Bitcoin",
                    status=MarketStatus.ACTIVE,
                ),
            )

    service = SearchServiceStub()
    application.dependency_overrides[get_market_list_service] = lambda: service

    with TestClient(application) as client:
        response = client.get(
            "/api/coins/search?query=btc",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert response.status_code == 200
    assert response.json()["data"][0]["marketCode"] == "KRW-BTC"
    assert service.calls == ["btc"]
    assert application.state.supabase_http_client is None
