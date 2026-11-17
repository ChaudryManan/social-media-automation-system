"""Structured JSON logging configuration and context tracking."""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Record attributes copied into the JSON line when a log call passes them via
# ``extra=``. This is an allow-list on purpose: other ``extra=`` values in the
# code base can contain personal data (for example email addresses) and must
# never be written to the logs.
STRUCTURED_FIELDS = (
    "method",
    "path",
    "route",
    "status_code",
    "duration_ms",
    "client_ip",
)

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
run_id_ctx: ContextVar[str | None] = ContextVar("run_id", default=None)
node_ctx: ContextVar[str | None] = ContextVar("node", default=None)
attempt_ctx: ContextVar[int | None] = ContextVar("attempt", default=None)


class JSONFormatter(logging.Formatter):
    """Format log records as structured JSON."""

    def format(self, record: logging.LogRecord) -> str:
        log_payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }

        # Context-based identifiers
        request_id = request_id_ctx.get()
        if request_id is not None:
            log_payload["request_id"] = request_id

        run_id = run_id_ctx.get()
        if run_id is not None:
            log_payload["run_id"] = run_id

        node = node_ctx.get()
        if node is not None:
            log_payload["node"] = node

        attempt = attempt_ctx.get()
        if attempt is not None:
            log_payload["attempt"] = attempt

        # Explicit record attributes fallback if supplied via extra
        if hasattr(record, "request_id") and "request_id" not in log_payload:
            log_payload["request_id"] = getattr(record, "request_id")

        for field in STRUCTURED_FIELDS:
            if hasattr(record, field):
                log_payload[field] = getattr(record, field)

        if record.exc_info:
            log_payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_payload, default=str)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure the root logger with the structured JSON formatter."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level.upper())
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    _route_uvicorn_logs_through_root()


def _route_uvicorn_logs_through_root() -> None:
    """Make Uvicorn's own logs JSON and silence its plain-text access log.

    Uvicorn installs its own handlers before the app is imported. Its startup
    and shutdown messages are re-routed to the JSON root handler, and the
    ``uvicorn.access`` logger is disabled so each request is logged exactly
    once, by ``AccessLogMiddleware`` (even if ``--no-access-log`` is forgotten).
    """
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True
