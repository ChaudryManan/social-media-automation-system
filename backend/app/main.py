"""Minimal FastAPI application and container health endpoints."""

import os

import psycopg
import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="Social Media Automation Platform")


def _database_is_available() -> bool:
    """Return whether PostgreSQL accepts a lightweight query."""
    database_url = os.getenv("DATABASE_URL")
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
    redis_url = os.getenv("REDIS_URL")
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


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the API process is alive."""
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> JSONResponse:
    """Report whether the API's database and Redis dependencies are ready."""
    checks = {
        "database": _database_is_available(),
        "redis": _redis_is_available(),
    }
    is_ready = all(checks.values())
    return JSONResponse(
        status_code=200 if is_ready else 503,
        content={"status": "ok" if is_ready else "not_ready", "checks": checks},
    )
