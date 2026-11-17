"""Access-log tests (B1-11): one structured JSON line per request, with request_id.

Most tests use a real ``create_app()`` instance so the production middleware order
is what gets tested; ``test_uvicorn_json_logs.py`` covers the real server process.
"""

import asyncio
import io
import json
import logging
import time
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from app.core.logging import JSONFormatter, setup_logging
from app.core.middleware import ACCESS_LOGGER_NAME, AccessLogMiddleware
from app.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

SECRET = "s3cr3t-value-that-must-never-be-logged"


class AccessLines:
    """Collects the JSON lines written to the access logger."""

    def __init__(self) -> None:
        self.stream = io.StringIO()

    @property
    def raw(self) -> str:
        return self.stream.getvalue()

    @property
    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.raw.splitlines() if line.strip()]

    def only(self) -> dict[str, Any]:
        records = self.records
        assert len(records) == 1, f"expected exactly one access line, got {records}"
        return records[0]


@pytest.fixture
def lines() -> Generator[AccessLines, None, None]:
    collected = AccessLines()
    handler = logging.StreamHandler(collected.stream)
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    previous = (logger.level, logger.propagate)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield collected
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous[0])
        logger.propagate = previous[1]


@pytest.fixture
def app() -> FastAPI:
    application = create_app()

    @application.get("/items/{item_id}")
    def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    @application.get("/boom")
    def boom() -> None:
        raise RuntimeError(f"internal detail {SECRET}")

    @application.get("/slow")
    def slow() -> dict[str, bool]:
        time.sleep(0.06)
        return {"ok": True}

    @application.post("/echo")
    def echo(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


# ----------------------------------------------------------------- basic shape


def test_each_request_writes_exactly_one_json_line_with_all_fields(
    client: TestClient, lines: AccessLines
) -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-123"})

    assert response.status_code == 200
    record = lines.only()
    assert record["logger"] == ACCESS_LOGGER_NAME
    assert record["level"] == "INFO"
    assert record["message"] == "GET /health 200"
    assert record["request_id"] == "trace-123"
    assert record["method"] == "GET"
    assert record["path"] == "/health"
    assert record["route"] == "/health"
    assert record["status_code"] == 200
    assert isinstance(record["duration_ms"], int | float) and record["duration_ms"] >= 0
    assert record["client_ip"]
    assert record["timestamp"]


def test_generated_request_id_matches_the_response_header(
    client: TestClient, lines: AccessLines
) -> None:
    response = client.get("/health")

    request_id = lines.only()["request_id"]
    assert request_id == response.headers["x-request-id"]
    assert str(uuid.UUID(request_id)) == request_id


def test_every_request_gets_its_own_line_and_id(
    client: TestClient, lines: AccessLines
) -> None:
    for _ in range(3):
        client.get("/health")

    ids = [record["request_id"] for record in lines.records]
    assert len(ids) == 3 and len(set(ids)) == 3


def test_route_is_the_template_not_the_concrete_path(
    client: TestClient, lines: AccessLines
) -> None:
    client.get("/items/42")

    record = lines.only()
    assert record["path"] == "/items/42"
    assert record["route"] == "/items/{item_id}"


def test_api_v1_routes_report_their_full_template(
    client: TestClient, lines: AccessLines
) -> None:
    client.get("/api/v1/workspaces")  # 401, fails before any database access

    record = lines.only()
    assert record["status_code"] == 401
    assert record["route"] == "/api/v1/workspaces"


# --------------------------------------------------------- statuses and levels


@pytest.mark.parametrize(
    ("path", "status", "level"),
    [
        ("/health", 200, "INFO"),
        ("/api/v1/workspaces", 401, "INFO"),
        ("/items/not-a-number", 422, "INFO"),
        ("/does-not-exist", 404, "INFO"),
    ],
)
def test_client_errors_and_successes_are_info(
    client: TestClient, lines: AccessLines, path: str, status: int, level: str
) -> None:
    client.get(path)

    record = lines.only()
    assert record["status_code"] == status
    assert record["level"] == level


def test_unmatched_path_has_no_route(client: TestClient, lines: AccessLines) -> None:
    client.get("/does-not-exist")

    assert "route" not in lines.only()


def test_unhandled_exception_is_logged_once_as_500_error(
    client: TestClient, lines: AccessLines
) -> None:
    response = client.get("/boom", headers={"X-Request-ID": "trace-boom"})

    assert response.status_code == 500
    record = lines.only()
    assert record["status_code"] == 500
    assert record["level"] == "ERROR"
    assert record["request_id"] == "trace-boom"
    assert SECRET not in lines.raw  # exception text is not part of the access line


def test_duration_covers_the_time_spent_in_the_handler(
    client: TestClient, lines: AccessLines
) -> None:
    client.get("/slow")

    assert lines.only()["duration_ms"] >= 50


def test_cors_preflight_is_logged_and_carries_a_request_id(
    client: TestClient, lines: AccessLines
) -> None:
    response = client.options(
        "/api/v1/workspaces",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    record = lines.only()
    assert record["method"] == "OPTIONS"
    assert record["request_id"] == response.headers["x-request-id"]


# ------------------------------------------------------ nothing sensitive leaks


def test_query_string_headers_and_bodies_are_never_logged(
    client: TestClient, lines: AccessLines
) -> None:
    client.post(
        f"/echo?token={SECRET}&password={SECRET}",
        json={"password": SECRET, "refresh_token": SECRET},
        headers={
            "Authorization": f"Bearer {SECRET}",
            "Cookie": f"session={SECRET}",
        },
    )

    assert SECRET not in lines.raw
    record = lines.only()
    assert record["path"] == "/echo"
    assert set(record) == {
        "timestamp",
        "level",
        "message",
        "logger",
        "request_id",
        "method",
        "path",
        "route",
        "status_code",
        "duration_ms",
        "client_ip",
    }


def test_very_long_paths_are_truncated(client: TestClient, lines: AccessLines) -> None:
    client.get("/" + "a" * 5000)

    assert len(lines.only()["path"]) == 2048


def test_control_characters_in_the_path_cannot_forge_log_lines(
    client: TestClient, lines: AccessLines
) -> None:
    client.get("/x%0A%7B%22forged%22%3A1%7D")

    assert len(lines.raw.splitlines()) == 1  # still one physical line
    assert "forged" in lines.only()["path"]


# ------------------------------------------- ASGI edge cases (no real server)


def _http_scope(**overrides: Any) -> dict[str, Any]:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/edge",
        "headers": [],
        "client": ("203.0.113.7", 5555),
    }
    scope.update(overrides)
    return scope


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _noop_send(_message: dict[str, Any]) -> None:
    return None


def test_cancelled_request_is_logged_as_a_warning_without_a_status(
    lines: AccessLines,
) -> None:
    async def app(_scope: Any, _receive: Any, _send: Any) -> None:
        raise asyncio.CancelledError

    middleware = AccessLogMiddleware(app)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(middleware(_http_scope(), _noop_receive, _noop_send))

    record = lines.only()
    assert record["level"] == "WARNING"
    assert record["status_code"] is None
    assert record["client_ip"] == "203.0.113.7"


def test_error_after_the_response_started_keeps_the_real_status(
    lines: AccessLines,
) -> None:
    async def app(_scope: Any, _receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("stream broke")

    middleware = AccessLogMiddleware(app)
    with pytest.raises(RuntimeError):
        asyncio.run(middleware(_http_scope(), _noop_receive, _noop_send))

    record = lines.only()
    assert record["status_code"] == 200
    assert record["level"] == "ERROR"


def test_non_http_scopes_pass_through_without_a_log_line(lines: AccessLines) -> None:
    seen: list[str] = []

    async def app(scope: Any, _receive: Any, _send: Any) -> None:
        seen.append(scope["type"])

    middleware = AccessLogMiddleware(app)
    for scope_type in ("lifespan", "websocket"):
        asyncio.run(middleware({"type": scope_type}, _noop_receive, _noop_send))

    assert seen == ["lifespan", "websocket"]
    assert lines.raw == ""


def test_missing_client_is_simply_left_out(lines: AccessLines) -> None:
    async def app(_scope: Any, _receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = AccessLogMiddleware(app)
    scope = _http_scope()
    del scope["client"]
    asyncio.run(middleware(scope, _noop_receive, _noop_send))

    record = lines.only()
    assert record["status_code"] == 204
    assert "client_ip" not in record


# ---------------------------------------------------------- logging configuration


def test_formatter_only_emits_allow_listed_extra_fields() -> None:
    """Existing ``extra=`` calls carry emails; they must stay out of the logs."""
    logger = logging.getLogger("privacy-check")
    record = logger.makeRecord(
        "privacy-check",
        logging.INFO,
        "f.py",
        1,
        "User registered",
        (),
        None,
        extra={"email": "person@example.com", "to": "x@y.z", "status_code": 201},
    )

    parsed = json.loads(JSONFormatter().format(record))

    assert parsed["status_code"] == 201
    assert "email" not in parsed and "to" not in parsed
    assert "person@example.com" not in json.dumps(parsed)


def test_setup_logging_makes_uvicorn_logs_json_and_silences_its_access_log() -> None:
    setup_logging("INFO")

    access = logging.getLogger("uvicorn.access")
    assert access.disabled and not access.propagate and not access.handlers
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        assert uvicorn_logger.propagate and not uvicorn_logger.handlers
    root_handlers = logging.getLogger().handlers
    assert len(root_handlers) == 1
    assert isinstance(root_handlers[0].formatter, JSONFormatter)


def test_running_alembic_in_process_does_not_disable_app_logging() -> None:
    """Regression: env.py's fileConfig() used to disable every existing logger."""
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[2]
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "backend" / "alembic"))
    config.output_buffer = io.StringIO()  # offline mode: prints SQL, no database
    setup_logging("INFO")
    handlers_before = list(logging.getLogger().handlers)

    command.upgrade(config, "head", sql=True)

    assert logging.getLogger(ACCESS_LOGGER_NAME).disabled is False
    assert logging.getLogger("app.services.auth").disabled is False
    assert logging.getLogger().handlers == handlers_before
