import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.errors import (
    ERROR_STATUS_CODES,
    AppError,
    AuthenticationError,
    ErrorCode,
    INVALID_REQUEST_MESSAGE,
)
from app.schemas.common import ErrorContent, ErrorResponse


__all__ = ["AuthenticationError", "register_exception_handlers"]

logger = logging.getLogger("uvicorn.error")


def _error_response(
    *,
    code: ErrorCode,
    message: str,
    details: object | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    response = ErrorResponse(
        error=ErrorContent(code=code, message=message, details=details)
    )
    return JSONResponse(
        status_code=ERROR_STATUS_CODES[code],
        headers=headers,
        content=response.model_dump(mode="json"),
    )


async def app_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del request
    if not isinstance(exc, AppError):
        raise TypeError("Expected AppError")
    return _error_response(
        code=exc.code,
        message=exc.message,
        details=exc.details,
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    del request
    if not isinstance(exc, RequestValidationError):
        raise TypeError("Expected RequestValidationError")
    return _error_response(
        code=ErrorCode.INVALID_REQUEST,
        message=INVALID_REQUEST_MESSAGE,
    )


async def unexpected_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    logger.error(
        "Unhandled exception while processing %s %s",
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return _error_response(
        code=ErrorCode.INTERNAL_SERVER_ERROR,
        message="서버 내부 오류가 발생했습니다.",
    )


def register_exception_handlers(application: FastAPI) -> None:
    application.add_exception_handler(AppError, app_exception_handler)
    application.add_exception_handler(
        RequestValidationError, validation_exception_handler
    )
    application.add_exception_handler(Exception, unexpected_exception_handler)
