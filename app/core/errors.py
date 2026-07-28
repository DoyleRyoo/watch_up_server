from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from fastapi import status


INVALID_REQUEST_MESSAGE: Final = "요청값이 올바르지 않습니다."


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_MARKET_CODE = "INVALID_MARKET_CODE"
    WATCHLIST_LIMIT_EXCEEDED = "WATCHLIST_LIMIT_EXCEEDED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    FORBIDDEN = "FORBIDDEN"
    WATCHLIST_NOT_FOUND = "WATCHLIST_NOT_FOUND"
    WATCHLIST_DUPLICATED = "WATCHLIST_DUPLICATED"
    UPBIT_UNAVAILABLE = "UPBIT_UNAVAILABLE"
    UPBIT_RATE_LIMITED = "UPBIT_RATE_LIMITED"
    UPBIT_TEMPORARILY_BLOCKED = "UPBIT_TEMPORARILY_BLOCKED"
    REDIS_UNAVAILABLE = "REDIS_UNAVAILABLE"
    CACHE_REFRESH_IN_PROGRESS = "CACHE_REFRESH_IN_PROGRESS"
    INTERNAL_SERVER_ERROR = "INTERNAL_SERVER_ERROR"


ERROR_STATUS_CODES: Final[Mapping[ErrorCode, int]] = MappingProxyType(
    {
        ErrorCode.INVALID_REQUEST: status.HTTP_400_BAD_REQUEST,
        ErrorCode.INVALID_MARKET_CODE: status.HTTP_400_BAD_REQUEST,
        ErrorCode.WATCHLIST_LIMIT_EXCEEDED: status.HTTP_400_BAD_REQUEST,
        ErrorCode.AUTH_REQUIRED: status.HTTP_401_UNAUTHORIZED,
        ErrorCode.AUTH_TOKEN_EXPIRED: status.HTTP_401_UNAUTHORIZED,
        ErrorCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
        ErrorCode.WATCHLIST_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        ErrorCode.WATCHLIST_DUPLICATED: status.HTTP_409_CONFLICT,
        ErrorCode.UPBIT_UNAVAILABLE: status.HTTP_502_BAD_GATEWAY,
        ErrorCode.UPBIT_RATE_LIMITED: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.UPBIT_TEMPORARILY_BLOCKED: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.REDIS_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.CACHE_REFRESH_IN_PROGRESS: status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.INTERNAL_SERVER_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
    }
)


class AppError(Exception):
    """Expected application error with an immutable code-to-status mapping."""

    def __init__(
        self,
        *,
        code: ErrorCode,
        message: str,
        details: Any | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.headers = dict(headers) if headers is not None else None

    @property
    def status_code(self) -> int:
        return ERROR_STATUS_CODES[self.code]


class AuthenticationError(AppError):
    @classmethod
    def required(cls) -> "AuthenticationError":
        return cls(
            code=ErrorCode.AUTH_REQUIRED,
            message="인증이 필요합니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @classmethod
    def expired(cls) -> "AuthenticationError":
        return cls(
            code=ErrorCode.AUTH_TOKEN_EXPIRED,
            message="인증 토큰이 만료되었습니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )
