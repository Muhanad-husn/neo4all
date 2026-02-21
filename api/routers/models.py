"""
api/routers/models.py — Pydantic request/response models for health and
monitoring endpoints (SKILL-A R-A1, R-A2).

Models are co-located in the api/routers/ subdirectory and imported by the
route handlers in health.py and monitoring.py (SKILL-A R-A5). All models
extend BaseResponse to carry the standard run_id/status/errors envelope.
"""

from typing import Any

from pydantic import BaseModel

from api.models.responses import BaseResponse

# ---------------------------------------------------------------------------
# health.py models — GET /api/health
# ---------------------------------------------------------------------------


class ServiceStatus(BaseModel):
    """Connectivity status for a single backend service (no latency)."""

    name: str
    healthy: bool
    error: str | None = None


class HealthResponse(BaseResponse):
    """Response model for GET /api/health."""

    services: list[ServiceStatus]
    version: str


# ---------------------------------------------------------------------------
# monitoring.py models — GET /api/monitoring/*
# ---------------------------------------------------------------------------


class ServiceHealth(BaseModel):
    """Extended connectivity status with latency measurement."""

    name: str
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None


class ExtendedHealthResponse(BaseResponse):
    """Response model for GET /api/monitoring/health."""

    services: list[ServiceHealth]
    version: str


class LogsRecentResponse(BaseResponse):
    """Response model for GET /api/monitoring/logs/recent.

    entries contains raw log record dicts from the in-memory ring buffer.
    Each entry has at minimum: event, level, timestamp, correlation_id.
    Additional fields vary per log call site (SKILL-D R-D3).
    """

    entries: list[dict[str, Any]]
    total: int
    level_filter: str | None


class RunSummaryResponse(BaseResponse):
    """Response model for GET /api/monitoring/run/{run_id}.

    In SPEC-01, run state is managed client-side by the UI StateManager and
    is not persisted server-side. found=False until a server-side run
    registry is introduced in a later increment.
    """

    found: bool
    phase: str | None = None
    schema_version: str | None = None
