"""
api/routers/models.py — Pydantic request/response models for health and
monitoring endpoints (SKILL-A R-A1, R-A2).

SPEC-08 additions: CacheStatsResponse, ResponseTimePercentiles.

Models are co-located in the api/routers/ subdirectory and imported by the
route handlers in health.py, monitoring.py, and monitoring_agents.py
(SKILL-A R-A5). All models extend BaseResponse to carry the standard
run_id/status/errors envelope.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Agent telemetry models — GET /api/monitoring/metrics  (SPEC-07 S-07.10)
#                           GET /api/monitoring/agents/{run_id}
# ---------------------------------------------------------------------------


class AgentUsageSummary(BaseModel):
    """Aggregated LLM usage for a single agent type.

    Attributes:
        agent_name:       Agent identifier (orchestrator, agent-a, agent-b, agent-p).
        total_tokens_in:  Sum of prompt tokens across all invocations.
        total_tokens_out: Sum of completion tokens across all invocations.
        total_cost:       Sum of estimated USD cost across all invocations.
        invocation_count: Number of times this agent was invoked.
    """

    agent_name: str
    total_tokens_in: int
    total_tokens_out: int
    total_cost: float
    invocation_count: int


class AggregatedMetricsResponse(BaseResponse):
    """Response model for GET /api/monitoring/metrics.

    Returns aggregated LLM usage across all runs, broken down by agent type,
    plus a global total of candidates processed and response time percentiles.
    """

    agents: list[AgentUsageSummary]
    total_candidates_processed: int
    response_time: "ResponseTimePercentiles | None" = None


class AgentTelemetryOut(BaseModel):
    """Per-agent telemetry record returned in AgentTelemetryResponse.

    Mirrors AgentTelemetryRecord from api/observability/metrics.py but
    decoupled as a router-level output model (SKILL-A R-A1).
    """

    run_id: str
    candidate_id: str
    agent_name: str
    tokens_in: int
    tokens_out: int
    cost_estimate: float
    execution_time_ms: float
    evidence_score: float | None = None
    timestamp: str


class AgentTelemetryResponse(BaseResponse):
    """Response model for GET /api/monitoring/agents/{run_id}.

    Returns per-candidate agent chain telemetry for all candidates
    processed in the specified run.
    """

    records: list[AgentTelemetryOut]
    total: int


# ---------------------------------------------------------------------------
# Cache stats models — GET /api/monitoring/cache  (SPEC-08 S-08.3)
# ---------------------------------------------------------------------------


class CacheStatsResponse(BaseResponse):
    """Response model for GET /api/monitoring/cache.

    Combines Redis-level statistics (total keys, memory) with
    application-level cache hit/miss counters from MetricsCollector.

    Attributes:
        total_keys:        Number of keys in the current Redis database (DBSIZE).
        memory_used_bytes: Redis used_memory (bytes).
        memory_used_human: Redis used_memory_human (human-readable, e.g. "1.5M").
        hit_count:         Application-level cache hits (CacheClient.get() hits).
        miss_count:        Application-level cache misses (CacheClient.get() misses).
        hit_ratio:         hit_count / (hit_count + miss_count), 0.0 when no data.
    """

    total_keys: int = 0
    memory_used_bytes: int = 0
    memory_used_human: str = "0B"
    hit_count: int = 0
    miss_count: int = 0
    hit_ratio: float = 0.0


# ---------------------------------------------------------------------------
# Response time percentile models — SPEC-08 S-08.3
# ---------------------------------------------------------------------------


class ResponseTimePercentiles(BaseModel):
    """Response time distribution percentiles (milliseconds).

    Sourced from the in-memory MetricsCollector histogram for
    ``response_duration_ms``.
    """

    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
