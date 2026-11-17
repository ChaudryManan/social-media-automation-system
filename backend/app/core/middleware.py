"""Custom middlewares for request processing and context propagation."""

import logging
import time
import uuid

from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import request_id_ctx

ACCESS_LOGGER_NAME = "app.access"
MAX_LOGGED_PATH_CHARS = 2048

access_logger = logging.getLogger(ACCESS_LOGGER_NAME)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Ensure every HTTP request has an X-Request-ID and attach it to context."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        incoming_id = request.headers.get("X-Request-ID")
        if incoming_id and incoming_id.strip():
            request_id = incoming_id.strip()
        else:
            request_id = str(uuid.uuid4())

        token = request_id_ctx.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_ctx.reset(token)


class AccessLogMiddleware:
    """Write exactly one structured JSON access-log line per HTTP request.

    Fields: ``method``, ``path`` (no query string), ``route`` (the matched route
    template, when there is one), ``status_code``, ``duration_ms`` and
    ``client_ip``; ``request_id`` comes from the logging context set by
    ``RequestIDMiddleware``, which must wrap this middleware.

    Request and response bodies, headers and query strings are never logged.
    ``duration_ms`` runs from the start of the request until the app has
    finished sending its response (or failed).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code: int | None = None
        failed = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            failed = True
            raise
        finally:
            if failed and status_code is None:
                status_code = 500  # the server answers 500 for an unhandled error
            self._log(scope, status_code, failed, started)

    @staticmethod
    def _log(
        scope: Scope, status_code: int | None, failed: bool, started: float
    ) -> None:
        method = scope.get("method", "")
        path = str(scope.get("path", ""))[:MAX_LOGGED_PATH_CHARS]
        fields: dict[str, object] = {
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        route_template = getattr(scope.get("route"), "path", None)
        if route_template:
            fields["route"] = route_template
        client = scope.get("client")
        if client:
            fields["client_ip"] = client[0]

        if failed or (status_code is not None and status_code >= 500):
            level = logging.ERROR
        elif status_code is None:
            level = logging.WARNING  # client went away before a response was sent
        else:
            level = logging.INFO
        access_logger.log(level, "%s %s %s", method, path, status_code, extra=fields)
