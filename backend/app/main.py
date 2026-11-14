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

    # Mount API routers (both /auth and /api/v1/auth prefixes supported)
    application.include_router(auth_router)
    application.include_router(auth_router, prefix="/api/v1")
    application.include_router(admin_router)
    application.include_router(admin_router, prefix="/api/v1")
    application.include_router(workspaces_router)
    application.include_router(workspaces_router, prefix="/api/v1")

    @application.get("/health")
    def health() -> dict[str, str]:
        """Report that the API process is alive."""
        return {"status": "ok"}

    @application.get("/health/ready")
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
