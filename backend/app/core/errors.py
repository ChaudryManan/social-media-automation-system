"""Application error hierarchy and global exception handlers."""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base application error producing the standard error structure."""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = status.HTTP_400_BAD_REQUEST,
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details if details is not None else {}


class NotFoundError(AppError):
    def __init__(
        self,
        message: str = "Resource not found",
        code: str = "not_found",
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            http_status=status.HTTP_404_NOT_FOUND,
            details=details,
        )


class UnauthorizedError(AppError):
    def __init__(
        self,
        message: str = "Unauthorized",
        code: str = "unauthorized",
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            http_status=status.HTTP_401_UNAUTHORIZED,
            details=details,
        )


class ForbiddenError(AppError):
    def __init__(
        self,
        message: str = "Forbidden",
        code: str = "forbidden",
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            http_status=status.HTTP_403_FORBIDDEN,
            details=details,
        )


class ConflictError(AppError):
    def __init__(
        self,
        message: str = "Resource conflict",
        code: str = "conflict",
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            http_status=status.HTTP_409_CONFLICT,
            details=details,
        )


class ValidationError(AppError):
    def __init__(
        self,
        message: str = "Validation failed",
        code: str = "validation_error",
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details=details,
        )


class LimitExceededError(AppError):
    def __init__(
        self,
        message: str = "Limit exceeded",
        code: str = "limit_exceeded",
        http_status: int = status.HTTP_400_BAD_REQUEST,
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            http_status=http_status,
            details=details,
        )


class RateLimitError(AppError):
    def __init__(
        self,
        message: str = "Too many requests. Please try again later.",
        code: str = "rate_limit_exceeded",
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            http_status=status.HTTP_429_TOO_MANY_REQUESTS,
            details=details,
        )


class ServiceUnavailableError(AppError):
    def __init__(
        self,
        message: str = "Service temporarily unavailable. Please try again later.",
        code: str = "service_unavailable",
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
            details=details,
        )


class InternalError(AppError):
    def __init__(
        self,
        message: str = "Internal server error",
        code: str = "internal_error",
        details: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            details=details,
        )


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    """Handle AppError subclasses and serialize to standard error response."""
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Map FastAPI/Pydantic validation errors (422) to standard response."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )


async def http_exception_handler(
    _request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Map Starlette/FastAPI HTTP exceptions to standard error response."""
    code = "not_found" if exc.status_code == status.HTTP_404_NOT_FOUND else "http_error"
    message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": {},
            }
        },
    )


async def unhandled_exception_handler(
    _request: Request, exc: Exception
) -> JSONResponse:
    """Catch-all handler for unexpected internal exceptions."""
    logger.exception("Unhandled server error: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "internal_error",
                "message": "An unexpected error occurred",
                "details": {},
            }
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register all application and framework exception handlers."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
