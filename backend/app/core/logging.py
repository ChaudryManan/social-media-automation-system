"""Structured JSON logging configuration and context tracking."""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

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

        if record.exc_info:
            log_payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_payload)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure the root logger with the structured JSON formatter."""
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level.upper())
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
