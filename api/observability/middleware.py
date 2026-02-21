"""
api/observability/middleware.py — FastAPI request lifecycle middleware.

Registered in api/main.py via app.add_middleware(CorrelationMiddleware).

Responsibilities (SKILL-D R-D2, R-D3):
  1. Clear stale structlog context vars at the start of each request so context
     from a previous request on the same connection does not bleed through.
  2. Extract or generate a correlation ID and bind it to the async context.
     All log entries produced during the request carry the ID automatically.
  3. Emit a structured "request_started" event.
  4. Measure wall-clock duration with time.perf_counter().
  5. Emit "request_completed" (with status code + duration) or "request_failed"
     (with exc_info + duration) depending on outcome.

Log events produced
-------------------
  request_started   — INFO   — method, path, client
  request_completed — INFO   — method, path, status_code, duration_ms
  request_failed    — ERROR  — method, path, duration_ms, exc_info
"""

import time
from collections.abc import Callable
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from api.observability.correlation import (
    extract_from_headers,
    new_request_id,
    set_correlation_id,
)
from api.observability.logger import get_logger

logger = get_logger(__name__)


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Bind a correlation ID to every request and log the full lifecycle."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[..., Any],
    ) -> Response:
        # Step 1: Clear any context vars left over from connection reuse or a
        # previous coroutine on the same event-loop task.
        structlog.contextvars.clear_contextvars()

        # Step 2: Resolve correlation ID — prefer the run_id from the caller so
        # all log entries link directly to a governed Run record.
        correlation_id: str = (
            extract_from_headers(dict(request.headers)) or new_request_id()
        )
        set_correlation_id(correlation_id)

        # Step 3: Log request start.
        start: float = time.perf_counter()
        logger.info(
            "request_started",
            method=request.method,
            path=str(request.url.path),
            client=request.client.host if request.client else None,
        )

        # Step 4: Delegate to the route handler.
        try:
            response: Response = await call_next(request)
            duration_ms: float = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                "request_completed",
                method=request.method,
                path=str(request.url.path),
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
            return response

        except Exception as exc:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.error(
                "request_failed",
                method=request.method,
                path=str(request.url.path),
                duration_ms=duration_ms,
                exc_info=exc,
            )
            raise
