"""FastAPI application entrypoint with middleware and health endpoints."""

import psycopg
import redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.admin import router as admin_router
from app.api.v1.auth import router as auth_router
from app.api.v1.workspaces import router as workspaces_router
from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.logging import setup_logging
from app.core.middleware import RequestIDMiddleware

API_DESCRIPTION = """\
Backend API for the Social Media Multi-Agent Automation Platform.

**Base path:** every endpoint below is served under `/api/v1`.

**Authentication:** call `POST /api/v1/auth/login`, then click **Authorize** and \
paste the `access_token`. Access tokens are short-lived; use `/auth/refresh`.

**Errors:** every error has the shape \
`{"error": {"code": "...", "message": "...", "details": ...}}`. Branch on `code`, \
not on `message`. Validation errors are HTTP 422 with `code=validation_error`.

**Pagination:** list endpoints take `limit` and `offset` and return \
`{items, total, limit, offset}`.
"""

OPENAPI_TAGS = [
    {
        "name": "auth",
        "description": "Register, log in, refresh, log out, password reset.",
    },
    {
        "name": "workspaces",
        "description": "A user's brands/projects. Owner-only (admins bypass, audited).",
    },
    {"name": "admin", "description": "Administrator-only endpoints."},
    {
        "name": "health",
        "description": (
            "Liveness and readiness probes (served at the root, not under `/api/v1`)."
        ),
    },
]


def _database_is_available() -> bool:
    """Return whether PostgreSQL accepts a lightweight query."""
    database_url = settings.DATABASE_URL
    if not database_url:
        return False

    connection_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(connection_url, connect_timeout=1) as connection:
            connection.execute("SELECT 1")
    except (OSError, psycopg.Error):
        return False
    return True


def _redis_is_available() -> bool:
    """Return whether Redis accepts a ping."""
    redis_url = settings.REDIS_URL
    if not redis_url:
        return False

    client = redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        return bool(client.ping())
    except (OSError, redis.RedisError):
        return False
    finally:
        client.close()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    setup_logging(settings.LOG_LEVEL)

    application = FastAPI(
        title="Social Media Automation Platform",
        version="0.1.0",
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
    )

    # Middleware execution order: RequestIDMiddleware runs on all requests;
    # CORSMiddleware handles cross-origin preflight and headers.
    application.add_middleware(RequestIDMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    register_error_handlers(application)

    # Routers are served under /api/v1 (the documented, canonical path) and, for
    # backwards compatibility, also at the root. The root copies are hidden from
    # OpenAPI so /docs lists every route exactly once.
    for router in (auth_router, admin_router, workspaces_router):
        application.include_router(router, prefix="/api/v1")
        application.include_router(router, include_in_schema=False)

    @application.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Report that the API process is alive."""
        return {"status": "ok"}

    @application.get(
        "/health/ready",
        tags=["health"],
        responses={
            503: {
                "description": "A dependency is down.",
                "content": {
                    "application/json": {
                        "example": {
                            "status": "not_ready",
                            "checks": {"database": True, "redis": False},
                        }
                    }
                },
            }
        },
    )
    def ready() -> JSONResponse:
        """Report whether API database and Redis dependencies are ready."""
        checks = {
            "database": _database_is_available(),
            "redis": _redis_is_available(),
        }
        is_ready = all(checks.values())
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ok" if is_ready else "not_ready",
                "checks": checks,
            },
        )

    return application


app = create_app()
