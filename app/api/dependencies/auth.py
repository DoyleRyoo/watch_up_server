"""Bearer JWT 검증과 사용자 범위 Supabase Client를 FastAPI에 제공한다."""

from typing import Annotated

import httpx
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from supabase import Client

from app.clients.supabase import (
    SUPABASE_DATA_API_TIMEOUT_SECONDS,
    create_user_supabase_client,
)
from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.security import AuthenticationConfigurationError, JWTVerifier
from app.models.auth import AuthContext


bearer_scheme = HTTPBearer(auto_error=False)


def extract_bearer_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ] = None,
) -> str:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise AuthenticationError.required()
    token = credentials.credentials.strip()
    if not token or any(character.isspace() for character in token):
        raise AuthenticationError.required()
    return token


def _authentication_settings(settings: Settings) -> tuple[str, str, str]:
    values = (
        settings.supabase_jwks_url.strip(),
        settings.supabase_issuer.strip(),
        settings.supabase_audience.strip(),
    )
    if not all(values):
        raise AuthenticationConfigurationError(
            "Supabase authentication settings are incomplete"
        )
    return values


def get_jwt_verifier(request: Request) -> JWTVerifier:
    """프로세스의 JWKS cache를 공유하는 verifier를 지연 생성한다."""
    existing_verifier: JWTVerifier | None = request.app.state.jwt_verifier
    if existing_verifier is not None:
        return existing_verifier

    with request.app.state.jwt_verifier_lock:
        existing_verifier = request.app.state.jwt_verifier
        if existing_verifier is not None:
            return existing_verifier

        settings: Settings = request.app.state.settings
        jwks_url, issuer, audience = _authentication_settings(settings)
        verifier = JWTVerifier(
            jwks_url=jwks_url,
            issuer=issuer,
            audience=audience,
        )
        request.app.state.jwt_verifier = verifier
        return verifier


def get_auth_context(
    access_token: Annotated[str, Depends(extract_bearer_token)],
    verifier: Annotated[JWTVerifier, Depends(get_jwt_verifier)],
) -> AuthContext:
    return verifier.verify(access_token)


def get_supabase_http_client(request: Request) -> httpx.Client:
    """연결 풀만 공유하고 사용자 Authorization 상태는 저장하지 않는다."""
    existing_client: httpx.Client | None = request.app.state.supabase_http_client
    if existing_client is not None:
        return existing_client

    with request.app.state.supabase_http_client_lock:
        existing_client = request.app.state.supabase_http_client
        if existing_client is not None:
            return existing_client

        http_client = httpx.Client(timeout=SUPABASE_DATA_API_TIMEOUT_SECONDS)
        request.app.state.supabase_http_client = http_client
        return http_client


def get_supabase_client(
    request: Request,
    auth_context: Annotated[AuthContext, Depends(get_auth_context)],
    http_client: Annotated[httpx.Client, Depends(get_supabase_http_client)],
) -> Client:
    """검증된 현재 사용자의 token이 고정된 요청 전용 Data API Client를 만든다."""
    settings: Settings = request.app.state.settings
    return create_user_supabase_client(
        settings=settings,
        auth_context=auth_context,
        http_client=http_client,
    )
