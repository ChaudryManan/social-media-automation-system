"""Tests for request ID middleware, JSON logging, and CORS headers."""

import io
import json
import logging
import uuid

from app.core.logging import JSONFormatter, request_id_ctx
from app.core.middleware import RequestIDMiddleware
from app.main import app
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_request_id_generated_when_missing() -> None:
    """When X-Request-ID is missing, a new valid UUID is generated and returned."""
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert "x-request-id" in response.headers
    request_id = response.headers["x-request-id"]
    parsed_uuid = uuid.UUID(request_id)
    assert str(parsed_uuid) == request_id


def test_request_id_preserved_when_provided() -> None:
    """When X-Request-ID is provided in request, it must be echoed in response."""
    client = TestClient(app)
    custom_id = "custom-req-trace-12345"
    response = client.get("/health", headers={"X-Request-ID": custom_id})

    assert response.status_code == 200
    assert response.headers.get("x-request-id") == custom_id


def test_json_formatter_outputs_valid_json() -> None:
    """JSONFormatter should format log records as valid JSON with fields."""
    formatter = JSONFormatter()
    logger = logging.getLogger("test_logger")
    record = logger.makeRecord(
        name="test_logger",
        level=logging.INFO,
        fn="test_fn.py",
        lno=42,
        msg="Sample log message",
        args=(),
        exc_info=None,
    )

    token = request_id_ctx.set("ctx-request-id-abc")
    try:
        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "INFO"
        assert parsed["message"] == "Sample log message"
        assert parsed["logger"] == "test_logger"
        assert parsed["request_id"] == "ctx-request-id-abc"
        assert "timestamp" in parsed
    finally:
        request_id_ctx.reset(token)


def test_request_id_included_in_logs_during_request() -> None:
    """Logs emitted inside request handlers should include request_id."""
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setFormatter(JSONFormatter())

    test_logger = logging.getLogger("test_in_request")
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)

    test_app = FastAPI()
    test_app.add_middleware(RequestIDMiddleware)

    @test_app.get("/log-test")
    def log_endpoint() -> dict[str, str]:
        test_logger.info("Executing endpoint logic")
        return {"status": "ok"}

    client = TestClient(test_app)
    req_id = "test-log-propagation-id"
    response = client.get("/log-test", headers={"X-Request-ID": req_id})

    assert response.status_code == 200
    log_output = log_capture.getvalue()
    assert log_output.strip() != ""
    log_record = json.loads(log_output.strip().splitlines()[-1])
    assert log_record["request_id"] == req_id
    assert log_record["message"] == "Executing endpoint logic"


def test_cors_headers_and_expose_request_id() -> None:
    """CORS origins should be handled and X-Request-ID exposed to callers."""
    client = TestClient(app)
    # Preflight request
    headers = {
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Request-ID",
    }
    response = client.options("/health", headers=headers)
    assert response.status_code == 200
    assert (
        response.headers.get("access-control-allow-origin") == "http://localhost:3000"
    )

    # Actual request
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert (
        response.headers.get("access-control-allow-origin") == "http://localhost:3000"
    )
    expose = response.headers.get("access-control-expose-headers", "").lower()
    assert "x-request-id" in expose
