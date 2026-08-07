"""Supabase JWKS로 access token의 서명과 필수 claim을 검증한다."""

from collections.abc import Sequence
from threading import Lock
from time import monotonic
from typing import Final
from uuid import UUID

import jwt
from jwt import PyJWK, PyJWKClient
from jwt.exceptions import (
    ExpiredSignatureError,
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
    PyJWKError,
)

from app.core.exceptions import AuthenticationError
from app.models.auth import AuthContext


ALLOWED_JWT_ALGORITHMS: Final[tuple[str, ...]] = ("ES256", "RS256")
JWKS_CACHE_TTL_SECONDS: Final[float] = 300
JWKS_TIMEOUT_SECONDS: Final[float] = 5
UNKNOWN_KID_CACHE_TTL_SECONDS: Final[float] = 30
MAX_UNKNOWN_KID_CACHE_SIZE: Final[int] = 32


class AuthenticationConfigurationError(RuntimeError):
    """서버 설정 누락으로 인증 자체를 수행할 수 없을 때 발생한다."""


class JWTVerificationUnavailableError(RuntimeError):
    """설정된 JWKS endpoint에서 사용 가능한 공개키를 얻지 못했음을 나타낸다."""


class JWTVerifier:
    """크기가 제한된 원격 JWKS cache로 Supabase JWT를 검증한다."""

    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        audience: str,
        algorithms: Sequence[str] = ALLOWED_JWT_ALGORITHMS,
        cache_ttl_seconds: float = JWKS_CACHE_TTL_SECONDS,
        timeout_seconds: float = JWKS_TIMEOUT_SECONDS,
        unknown_kid_ttl_seconds: float = UNKNOWN_KID_CACHE_TTL_SECONDS,
        jwks_client: PyJWKClient | None = None,
    ) -> None:
        if not jwks_url or not issuer or not audience:
            raise AuthenticationConfigurationError(
                "Supabase authentication settings are incomplete"
            )
        if not algorithms:
            raise AuthenticationConfigurationError(
                "At least one JWT algorithm must be configured"
            )

        self._issuer = issuer
        self._audience = audience
        self._algorithms = tuple(algorithms)
        self._jwks_client = jwks_client or PyJWKClient(
            jwks_url,
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=cache_ttl_seconds,
            timeout=timeout_seconds,
        )
        self._key_lookup_lock = Lock()
        self._unknown_kid_ttl_seconds = unknown_kid_ttl_seconds
        self._unknown_key_ids: dict[str, float] = {}

    def verify(self, access_token: str) -> AuthContext:
        """검증된 UUID `sub`와 원본 token을 요청 범위 인증 문맥으로 반환한다.

        서명·만료·issuer·audience 중 하나라도 맞지 않으면 payload를 신뢰하지 않는다.
        만료만 `AUTH_TOKEN_EXPIRED`로 구분하고 나머지 token 오류는 내부 원인을 숨긴다.
        """
        try:
            header = jwt.get_unverified_header(access_token)
        except InvalidTokenError as exc:
            raise AuthenticationError.required() from exc

        algorithm = header.get("alg")
        key_id = header.get("kid")
        if algorithm not in self._algorithms:
            raise AuthenticationError.required()
        if not isinstance(key_id, str) or not key_id.strip():
            raise AuthenticationError.required()

        signing_key = self._find_signing_key(key_id)

        try:
            claims = jwt.decode(
                access_token,
                key=signing_key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except ExpiredSignatureError as exc:
            raise AuthenticationError.expired() from exc
        except InvalidTokenError as exc:
            raise AuthenticationError.required() from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthenticationError.required()
        try:
            user_id = UUID(subject)
        except (ValueError, AttributeError) as exc:
            raise AuthenticationError.required() from exc

        return AuthContext(user_id=user_id, access_token=access_token)

    def _find_signing_key(self, key_id: str) -> PyJWK:
        # PyJWKClient가 5분 JWKS cache를 소유한다. 이 lock은 같은 프로세스의 동시
        # cache miss가 Supabase에 중복 갱신 요청을 보내지 않도록 직렬화한다.
        with self._key_lookup_lock:
            now = monotonic()
            unknown_until = self._unknown_key_ids.get(key_id)
            if unknown_until is not None and unknown_until > now:
                raise AuthenticationError.required()
            self._unknown_key_ids.pop(key_id, None)

            keys = self._get_signing_keys(refresh=False)
            signing_key = self._match_key(keys, key_id)
            if signing_key is not None:
                return signing_key

            # key rotation 가능성 때문에 cache에 없는 kid는 한 번만 강제 갱신한다.
            # 그래도 없으면 짧게 기억해 임의 kid 요청이 JWKS fetch를 반복하지 못하게 한다.
            refreshed_keys = self._get_signing_keys(refresh=True)
            signing_key = self._match_key(refreshed_keys, key_id)
            if signing_key is None:
                self._remember_unknown_key(key_id, now)
                raise AuthenticationError.required()
            return signing_key

    def _remember_unknown_key(self, key_id: str, now: float) -> None:
        if len(self._unknown_key_ids) >= MAX_UNKNOWN_KID_CACHE_SIZE:
            oldest_key = min(
                self._unknown_key_ids, key=self._unknown_key_ids.__getitem__
            )
            self._unknown_key_ids.pop(oldest_key, None)
        self._unknown_key_ids[key_id] = now + self._unknown_kid_ttl_seconds

    def _get_signing_keys(self, *, refresh: bool) -> list[PyJWK]:
        try:
            return self._jwks_client.get_signing_keys(refresh=refresh)
        except (
            PyJWKClientConnectionError,
            PyJWKClientError,
            PyJWKError,
            OSError,
            ValueError,
        ) as exc:
            raise JWTVerificationUnavailableError(
                "Supabase JWKS could not be loaded"
            ) from exc

    @staticmethod
    def _match_key(keys: Sequence[PyJWK], key_id: str) -> PyJWK | None:
        return next((key for key in keys if key.key_id == key_id), None)
