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


# ---------------------------------------------------------------------------
# Worker monitoring models — GET /api/monitoring/workers (SPEC-04 S-04.8)
#                            GET /api/monitoring/jobs/{run_id}
# ---------------------------------------------------------------------------


class WorkerJobDetail(BaseModel):
    """Per-chunk extraction job detail returned in JobStatusResponse.

    Attributes:
        chunk_id:      Source chunk identifier (safe to log — never raw text).
        doc_id:        Parent document identifier.
        status:        Job lifecycle state: queued | running | complete | failed.
        error:         Error message set on "failed"; None otherwise.
        nodes_created: Nodes created in Neo4j on "complete".
        edges_created: Edges created in Neo4j on "complete".
        started_at:    ISO 8601 UTC timestamp when the job started running.
        completed_at:  ISO 8601 UTC timestamp when the job finished (any status).
    """

    chunk_id: str
    doc_id: str
    status: str
    error: str | None = None
    nodes_created: int = 0
    edges_created: int = 0
    started_at: str | None = None
    completed_at: str | None = None


class WorkerStatusResponse(BaseResponse):
    """Response model for GET /api/monitoring/workers.

    Attributes:
        queue_depth:  Number of jobs waiting in the ARQ sorted-set queue.
        active_jobs:  Number of chunk jobs currently in "running" state across
                      all runs (derived from Redis job-status keys).
        worker_count: Number of live ARQ worker processes (counted via Redis
                      health-check keys written by each worker with a TTL).
    """

    queue_depth: int = 0
    active_jobs: int = 0
    worker_count: int = 0


class JobStatusResponse(BaseResponse):
    """Response model for GET /api/monitoring/jobs/{run_id}.

    Attributes:
        jobs:      Per-chunk job details scanned from Redis for this run.
        total:     Total number of job records found in Redis.
        completed: Jobs with status "complete".
        failed:    Jobs with status "failed".
        pending:   Jobs with status "queued" or "running".
    """

    jobs: list[WorkerJobDetail] = []
    total: int = 0
    completed: int = 0
    failed: int = 0
    pending: int = 0
