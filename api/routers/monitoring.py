"""
api/routers/monitoring.py — GET /api/monitoring/*

Extended health with per-service latency, recent log retrieval, and
run-level summary. All business logic is delegated to services and
observability helpers (SKILL-B — no business logic in route handlers).

Endpoints introduced in SPEC-01 (SKILL-D R-D11):
    GET /api/monitoring/health            — per-service latency + connectivity
    GET /api/monitoring/logs/recent       — recent entries from ring buffer
    GET /api/monitoring/run/{run_id}      — run-level phase/schema summary
"""

from fastapi import APIRouter, Query

from api.config import get_settings
from api.observability.logger import get_logger, get_recent_logs
from api.routers.models import (
    ExtendedHealthResponse,
    LogsRecentResponse,
    RunSummaryResponse,
    ServiceHealth,
)
from api.services.health import probe_all_services

logger = get_logger(__name__)

router = APIRouter(tags=["monitoring"])

_VERSION = "0.1.0"


@router.get("/health", response_model=ExtendedHealthResponse)
async def get_extended_health() -> ExtendedHealthResponse:
    """Return per-service connectivity with latency measurements.

    Runs the same probes as GET /api/health but exposes latency_ms per
    service for performance monitoring. Suitable for dashboards that need
    round-trip timing in addition to up/down status.
    """
    settings = get_settings()
    results = await probe_all_services(settings)

    services = [
        ServiceHealth(
            name=r.name,
            healthy=r.healthy,
            latency_ms=r.latency_ms,
            error=r.error,
        )
        for r in results
    ]
    all_healthy = all(s.healthy for s in services)
    overall = "success" if all_healthy else "partial"

    logger.info("monitoring_health_checked", all_healthy=all_healthy, service_count=len(services))

    return ExtendedHealthResponse(
        run_id="",
        status=overall,
        services=services,
        version=_VERSION,
    )


@router.get("/logs/recent", response_model=LogsRecentResponse)
async def get_recent_log_entries(
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Maximum number of entries to return (newest first).",
    ),
    level: str | None = Query(
        default=None,
        description=(
            "Minimum severity filter. One of: DEBUG, INFO, WARNING, ERROR, CRITICAL. "
            "Returns entries at or above the specified level. "
            "Omit to return all levels."
        ),
    ),
) -> LogsRecentResponse:
    """Return recent log entries from the in-memory ring buffer.

    Entries are ordered newest-first. The optional level filter returns
    entries at or above the specified severity (e.g. level=WARNING returns
    WARNING, ERROR, and CRITICAL entries). Up to 1 000 entries per call.

    The ring buffer holds the last 1 000 log records across all components.
    No external store is queried — reads are O(n) over the buffer snapshot
    (SKILL-D R-D13).
    """
    entries = get_recent_logs(limit=limit, level=level)

    logger.debug("logs_recent_fetched", count=len(entries), level_filter=level)

    return LogsRecentResponse(
        run_id="",
        status="success",
        entries=entries,
        total=len(entries),
        level_filter=level,
    )


@router.get("/run/{run_id}", response_model=RunSummaryResponse)
async def get_run_summary(run_id: str) -> RunSummaryResponse:
    """Return a summary for the specified run.

    In SPEC-01, run state is managed client-side by the Streamlit
    StateManager and is not persisted to a server-side registry. This
    endpoint returns found=False for all queries. A server-side run
    registry (with phase, schema_version, document counts, and job counts)
    will be introduced in a later increment.
    """
    logger.info("run_summary_requested", run_id=run_id)

    return RunSummaryResponse(
        run_id=run_id,
        status="success",
        found=False,
    )
