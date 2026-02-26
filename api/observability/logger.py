"""
api/observability/logger.py — Centralized structlog factory (SKILL-D R-D1).

All modules obtain loggers exclusively from get_logger(__name__). No module may
call logging.basicConfig() or create its own logger configuration — CLAUDE.md §15.

call configure_logging() once at application startup (api/main.py) before the
ASGI server begins serving requests. Loggers created before configure_logging()
work correctly because structlog evaluates its configuration lazily on the first
log call.

Ring buffer
-----------
Every log record is also written to an in-memory ring buffer (maxlen=1000) so
the monitoring endpoint GET /api/monitoring/logs/recent can return recent entries
without querying any external store (SKILL-D R-D13).

Stdlib integration
------------------
structlog wraps the stdlib root logger so that third-party libraries (uvicorn,
neo4j driver, qdrant-client, etc.) that log via stdlib are processed by the same
pipeline and land in the same ring buffer.
"""

import logging
import sys
from collections import deque
from threading import Lock
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Ring buffer — recent log entries for monitoring endpoint
# ---------------------------------------------------------------------------
_MAX_BUFFER: int = 1000
_LOG_BUFFER: deque[dict[str, Any]] = deque(maxlen=_MAX_BUFFER)
_BUFFER_LOCK: Lock = Lock()

# ---------------------------------------------------------------------------
# One-time configuration guard
# ---------------------------------------------------------------------------
_CONFIGURED: bool = False
_CONFIG_LOCK: Lock = Lock()

_LEVEL_RANKS: dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "warn": 30,
    "error": 40,
    "critical": 50,
}


# ---------------------------------------------------------------------------
# Processors
# ---------------------------------------------------------------------------

def _ring_buffer_sink(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Processor: capture a snapshot of each log record into the ring buffer.

    Runs before the final renderer so the dict still contains all structured
    fields. The monitoring endpoint reads from this buffer.

    Non-serializable structlog metadata (``_record``, ``_from_structlog``)
    is stripped before storage so that GET /api/monitoring/logs/recent can
    JSON-encode the entries without hitting serialization errors on foreign
    stdlib log records.
    """
    snapshot = dict(event_dict)
    # _record is a logging.LogRecord (non-serializable) injected by structlog
    # for foreign stdlib records; _from_structlog is a bool marker.
    snapshot.pop("_record", None)
    snapshot.pop("_from_structlog", None)
    with _BUFFER_LOCK:
        _LOG_BUFFER.appendleft(snapshot)
    return event_dict


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def configure_logging(log_format: str = "console", log_level: str = "INFO") -> None:
    """Configure structlog and the stdlib root logger for the process lifetime.

    Idempotent — subsequent calls are no-ops.

    Args:
        log_format: "json" for production structured output,
                    "console" for human-readable development output.
        log_level:  Minimum level to emit. One of:
                    DEBUG | INFO | WARNING | ERROR | CRITICAL
    """
    global _CONFIGURED
    with _CONFIG_LOCK:
        if _CONFIGURED:
            return

        numeric_level = getattr(logging, log_level.upper(), logging.INFO)

        # Processors shared between structlog loggers and foreign stdlib loggers.
        # The merge_contextvars processor injects the correlation_id (and any
        # other contextvars bound by the middleware) into every log entry.
        shared_processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            _ring_buffer_sink,
        ]

        if log_format == "json":
            renderer: Any = structlog.processors.JSONRenderer()
        else:
            renderer = structlog.dev.ConsoleRenderer(colors=False)

        # Configure structlog. The wrap_for_formatter processor hands off the
        # event dict to stdlib's ProcessorFormatter for final rendering.
        structlog.configure(
            processors=shared_processors
            + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        # ProcessorFormatter handles both structlog-routed records (via
        # remove_processors_meta) and foreign stdlib records (via foreign_pre_chain).
        formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.ExceptionRenderer(),
                renderer,
            ],
        )

        # Configure the stdlib root logger.
        # Avoids logging.basicConfig() as required by CLAUDE.md §15.
        root = logging.getLogger()
        root.setLevel(numeric_level)
        for existing_handler in root.handlers[:]:
            root.removeHandler(existing_handler)
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(numeric_level)
        handler.setFormatter(formatter)
        root.addHandler(handler)

        _CONFIGURED = True


def get_logger(name: str) -> Any:
    """Return a named structlog logger.

    Safe to call at module level — structlog lazy-evaluates configuration on
    the first actual log call.

    Args:
        name: Typically __name__ of the calling module.

    Usage:
        from api.observability.logger import get_logger
        logger = get_logger(__name__)
        logger.info("schema_locked", run_id=run_id, version=schema_version)
    """
    return structlog.get_logger(name)


def get_recent_logs(
    limit: int = 100,
    level: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent log records from the ring buffer (newest first).

    Args:
        limit: Maximum number of records to return.
        level: Optional minimum severity filter (case-insensitive).
               E.g. "WARNING" returns WARNING, ERROR, and CRITICAL.

    Returns:
        List of log record dicts. Newest entries are first.
    """
    min_rank = _LEVEL_RANKS.get(level.lower(), 0) if level else 0

    with _BUFFER_LOCK:
        snapshot = list(_LOG_BUFFER)  # copy while holding lock

    if min_rank:
        snapshot = [
            r
            for r in snapshot
            if _LEVEL_RANKS.get(str(r.get("level", "")).lower(), 0) >= min_rank
        ]

    return snapshot[:limit]
