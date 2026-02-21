"""
api/observability/correlation.py — Per-request correlation ID management.

Provides the correlation ID that propagates through every log entry within a
request or background ARQ job scope (SKILL-D R-D2).

Strategy
--------
1. If the incoming HTTP request carries an "X-Run-ID" header, that run_id
   becomes the correlation ID, linking log entries directly to a governed Run.
2. Otherwise a unique, request-scoped ID is generated.

The ID is stored in two places:
  - A ContextVar (_correlation_var) — so any code can read the current ID via
    get_correlation_id() without receiving it as a parameter.
  - structlog's contextvars — so the ID appears automatically in every log
    entry produced during the request, via the merge_contextvars processor
    configured in api/observability/logger.py.

Note on uuid4
-------------
uuid4() is used for generating request IDs here. This is intentional and
permitted: CLAUDE.md §5 bans uuid4() for *governed artifact* IDs (doc_id,
chunk_id, proposal_id, etc.). Request-scoped tracing IDs are not governed
artifacts — they only need uniqueness, not deterministic derivation.
"""

import uuid
from contextvars import ContextVar

import structlog

# HTTP header that callers set to attach a run_id to a request.
CORRELATION_HEADER: str = "X-Run-ID"

# Async-safe storage — each asyncio task inherits a copy of the context.
_correlation_var: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    """Return the correlation ID bound to the current async context.

    Returns an empty string when called outside a request/job context.
    """
    return _correlation_var.get()


def set_correlation_id(correlation_id: str) -> None:
    """Bind a correlation ID to the current async context.

    Writes to both the ContextVar (for get_correlation_id() callers) and to
    structlog's contextvars (for automatic log injection). Call this once per
    request from CorrelationMiddleware.

    Args:
        correlation_id: The run_id or generated request ID for this context.
    """
    _correlation_var.set(correlation_id)
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)


def new_request_id() -> str:
    """Generate a unique request-scoped correlation ID.

    Returns:
        32-character lowercase hex string (uuid4, no dashes).
    """
    return uuid.uuid4().hex


def extract_from_headers(headers: dict[str, str] | None) -> str | None:
    """Extract a run_id from HTTP request headers.

    Starlette normalises header names to lowercase, so the lookup is
    case-insensitive at the HTTP layer but the value is returned as-is.

    Args:
        headers: Request headers mapping (or None).

    Returns:
        The X-Run-ID header value if present, else None.
    """
    if not headers:
        return None
    # Starlette stores headers with lowercase keys.
    return headers.get(CORRELATION_HEADER.lower()) or headers.get(CORRELATION_HEADER)
