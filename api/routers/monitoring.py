"""
api/routers/monitoring.py — GET /api/monitoring/*

Extended health with per-service latency, recent log retrieval, run-level
summary, ARQ worker status, and per-run job tracking.
All business logic is delegated to services and observability helpers
(SKILL-B — no business logic in route handlers).

Endpoints introduced in SPEC-01 (SKILL-D R-D11):
    GET /api/monitoring/health            — per-service latency + connectivity
    GET /api/monitoring/logs/recent       — recent entries from ring buffer
    GET /api/monitoring/run/{run_id}      — run-level phase/schema summary

Endpoints added in SPEC-04 (SKILL-D R-D11):
    GET /api/monitoring/workers           — ARQ queue depth, worker count
    GET /api/monitoring/jobs/{run_id}     — per-chunk extraction job statuses

Architecture (SKILL-B: thin routers)
--------------------------------------
Worker and job data come directly from Redis via the ARQ pool. No S3 calls
are made here — job status keys are scanned by run-scoped Redis pattern.
The ARQ pool is a module-level lazy singleton (mirrors api/routers/extraction.py).
Duplication of _get_arq_pool() is a SKILL-B R-B7 item for governance
consolidation into api/common/arq_pool.py in a future increment.

Caching (SKILL-D)
-----------------
These are monitoring endpoints — read-only, fail-open (always HTTP 200).
No additional caching is applied: job-status data is already in Redis.

Sensitive data (SKILL-D R-D5)
------------------------------
REDIS_URL is never logged in full. chunk_id (a safe hash) is safe to log.
"""

import json
from typing import Any

from fastapi import APIRouter, Query

from api.config import get_settings
from api.models.responses import ErrorDetail
from api.observability.logger import get_logger, get_recent_logs
from api.routers.models import (
    ExtendedHealthResponse,
    JobStatusResponse,
    LogsRecentResponse,
    RunSummaryResponse,
    ServiceHealth,
    WorkerJobDetail,
    WorkerStatusResponse,
)
from api.services.health import probe_all_services
from api.worker.jobs import ChunkJobStatus

logger = get_logger(__name__)

router = APIRouter(tags=["monitoring"])

_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# ARQ pool — module-level lazy singleton for worker monitoring queries.
# Separate from api/routers/extraction.py's pool; both connect to the same
# Redis instance. Duplication noted as SKILL-B R-B7 item.
# ---------------------------------------------------------------------------

_arq_pool: Any = None
_QUEUE_NAME: str = "arq:queue"  # must match WorkerSettings.queue_name


async def _get_arq_pool() -> Any:
    """Return (or create) the monitoring-scoped ARQ Redis pool.

    Uses a module-level variable so a single connection pool is shared across
    all monitoring requests in the process. Created lazily on first call.

    Raises:
        Exception: If REDIS_URL is absent from Settings or Redis is unreachable.

    Log events:
        monitoring_arq_pool_created  INFO — redis_host
    """
    global _arq_pool
    if _arq_pool is None:
        from arq import create_pool as arq_create_pool
        from arq.connections import RedisSettings

        settings = get_settings()
        redis_url = settings.REDIS_URL
        _arq_pool = await arq_create_pool(
            RedisSettings.from_dsn(redis_url),
            default_queue_name=_QUEUE_NAME,
        )
        safe_host = redis_url.split("@")[-1] if "@" in redis_url else redis_url
        logger.info("monitoring_arq_pool_created", redis_host=safe_host)

    return _arq_pool


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


# ---------------------------------------------------------------------------
# GET /api/monitoring/workers  (SPEC-04 S-04.8)
# ---------------------------------------------------------------------------


@router.get(
    "/workers",
    response_model=WorkerStatusResponse,
    summary="Return ARQ worker queue depth and active worker count",
)
async def get_worker_status() -> WorkerStatusResponse:
    """Return real-time ARQ worker status sourced directly from Redis.

    Data sources (all from Redis — no database queries):
    - **queue_depth**: ``ZCARD arq:queue`` — jobs waiting to be processed.
    - **worker_count**: count of ``arq:health-check:*`` keys (each live ARQ
      worker writes one with a TTL; keys expire when the worker exits).
    - **active_jobs**: count of job-status keys globally whose ``status``
      field is ``"running"`` (bounded by the 24-hour job-status TTL).

    Always returns HTTP 200. Returns zeros if Redis is unavailable (fail-open
    for monitoring per SKILL-D R-D9). Logs ``worker_status_arq_unavailable``
    at ERROR if the pool cannot be created.

    Log events:
        monitoring_arq_pool_created      INFO  — redis_host (on first call)
        worker_status_queried            INFO  — queue_depth, active_jobs, worker_count
        worker_status_arq_unavailable    ERROR — error
        worker_queue_depth_failed        WARNING — error
        worker_health_check_scan_failed  WARNING — error
        worker_active_jobs_scan_failed   WARNING — error
    """
    logger.info("worker_status_requested")

    try:
        pool = await _get_arq_pool()
    except Exception as exc:
        logger.error("worker_status_arq_unavailable", error=str(exc))
        return WorkerStatusResponse(
            run_id="",
            status="error",
            errors=[ErrorDetail(code="arq_unavailable", message=str(exc))],
        )

    # --- Queue depth: sorted-set cardinality ---
    queue_depth = 0
    try:
        queue_depth = await pool.zcard(_QUEUE_NAME)
    except Exception as exc:
        logger.warning("worker_queue_depth_failed", error=str(exc))

    # --- Worker count: one health-check key per live worker (with TTL) ---
    worker_count = 0
    try:
        async for _ in pool.scan_iter(match="arq:health-check:*", count=100):
            worker_count += 1
    except Exception as exc:
        logger.warning("worker_health_check_scan_failed", error=str(exc))

    # --- Active jobs: job-status keys globally with status="running" ---
    # O(total job keys) but bounded by 24-hour TTL per SKILL-D R-D13.
    active_jobs = 0
    try:
        job_keys = [k async for k in pool.scan_iter(match="job:*", count=100)]
        if job_keys:
            raw_values = await pool.mget(*job_keys)
            for raw in raw_values:
                if raw is None:
                    continue
                try:
                    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                    if json.loads(text).get("status") == "running":
                        active_jobs += 1
                except (json.JSONDecodeError, AttributeError):
                    pass
    except Exception as exc:
        logger.warning("worker_active_jobs_scan_failed", error=str(exc))

    logger.info(
        "worker_status_queried",
        queue_depth=queue_depth,
        active_jobs=active_jobs,
        worker_count=worker_count,
    )

    return WorkerStatusResponse(
        run_id="",
        status="success",
        queue_depth=queue_depth,
        active_jobs=active_jobs,
        worker_count=worker_count,
    )


# ---------------------------------------------------------------------------
# GET /api/monitoring/jobs/{run_id}  (SPEC-04 S-04.8)
# ---------------------------------------------------------------------------


@router.get(
    "/jobs/{run_id}",
    response_model=JobStatusResponse,
    summary="Return per-chunk extraction job statuses for a run",
)
async def get_run_jobs(run_id: str) -> JobStatusResponse:
    """Return per-chunk extraction job statuses for the specified run.

    Scans Redis for all ``job:{run_id}:*`` keys and reads each
    ``ChunkJobStatus`` record written by the ARQ extraction job. This scan
    is constrained to the run's own key prefix so it does not enumerate
    unrelated job keys.

    Always returns HTTP 200. Returns an empty job list if Redis is
    unavailable (fail-open per SKILL-D R-D9). Individual parse failures for
    malformed Redis values are logged at WARNING and skipped.

    Log events:
        jobs_status_requested         INFO  — run_id
        monitoring_arq_pool_created   INFO  — redis_host (on first pool creation)
        jobs_status_arq_unavailable   ERROR — run_id, error
        job_status_parse_error        WARNING — run_id, error
        jobs_status_success           INFO  — run_id, total, completed, failed, pending
    """
    logger.info("jobs_status_requested", run_id=run_id)

    try:
        pool = await _get_arq_pool()
    except Exception as exc:
        logger.error("jobs_status_arq_unavailable", run_id=run_id, error=str(exc))
        return JobStatusResponse(
            run_id=run_id,
            status="error",
            errors=[ErrorDetail(code="arq_unavailable", message=str(exc))],
        )

    # Scan for all job-status keys scoped to this run.
    pattern = f"job:{run_id}:*"
    raw_keys = [k async for k in pool.scan_iter(match=pattern, count=100)]

    jobs: list[WorkerJobDetail] = []
    completed = failed = pending = 0

    if raw_keys:
        raw_values = await pool.mget(*raw_keys)
        for raw in raw_values:
            if raw is None:
                continue
            try:
                text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                chunk_status = ChunkJobStatus.model_validate_json(text)
                jobs.append(
                    WorkerJobDetail(
                        chunk_id=chunk_status.chunk_id,
                        doc_id=chunk_status.doc_id,
                        status=chunk_status.status,
                        error=chunk_status.error,
                        nodes_created=chunk_status.nodes_created,
                        edges_created=chunk_status.edges_created,
                        started_at=chunk_status.started_at,
                        completed_at=chunk_status.completed_at,
                    )
                )
                if chunk_status.status == "complete":
                    completed += 1
                elif chunk_status.status == "failed":
                    failed += 1
                else:
                    pending += 1
            except Exception as exc:
                logger.warning(
                    "job_status_parse_error", run_id=run_id, error=str(exc)
                )

    logger.info(
        "jobs_status_success",
        run_id=run_id,
        total=len(jobs),
        completed=completed,
        failed=failed,
        pending=pending,
    )

    return JobStatusResponse(
        run_id=run_id,
        status="success",
        jobs=jobs,
        total=len(jobs),
        completed=completed,
        failed=failed,
        pending=pending,
    )
