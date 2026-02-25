"""
api/routers/monitoring_agents.py — Agent telemetry and cache statistics endpoints.

SPEC-07 agent telemetry and SPEC-08 cache statistics, split from monitoring.py
for maintainability (SKILL-B R-B7).

Endpoints (SKILL-D R-D11):
    GET /api/monitoring/metrics           — aggregated LLM usage per agent type
    GET /api/monitoring/agents/{run_id}   — per-candidate agent chain telemetry
    GET /api/monitoring/cache             — Redis stats, app-level hit/miss

Architecture (SKILL-B: thin routers)
--------------------------------------
Agent telemetry is sourced from the in-memory telemetry store in
api/observability/metrics.py — no Redis or database queries required.

Cache stats combine Redis INFO/DBSIZE with application-level counters from
MetricsCollector.

All endpoints are read-only, fail-open (always HTTP 200). No business logic
beyond reading from metrics/cache stores (SKILL-D R-D13).

Sensitive data (SKILL-D R-D5)
------------------------------
REDIS_URL is never logged in full.
"""

import time

from fastapi import APIRouter

from api.cache.client import get_cache_client
from api.observability.logger import get_logger
from api.observability.metrics import (
    get_agent_telemetry,
    get_aggregated_metrics,
    get_metrics,
)
from api.routers.models import (
    AggregatedMetricsResponse,
    AgentTelemetryOut,
    AgentTelemetryResponse,
    AgentUsageSummary,
    CacheStatsResponse,
    ResponseTimePercentiles,
)

logger = get_logger(__name__)

router = APIRouter(tags=["monitoring-agents"])


def _observe_response_time(endpoint: str, start: float) -> None:
    """Record endpoint response duration in the in-memory histogram.

    Called at the end of each endpoint handler. The global histogram
    (no labels) is read by GET /api/monitoring/metrics to expose
    p50/p95/p99 percentiles. A per-endpoint counter is also incremented
    for optional breakdown analysis.
    """
    duration_ms = (time.monotonic() - start) * 1000
    get_metrics().observe("response_duration_ms", duration_ms)
    get_metrics().increment("response_count", endpoint=endpoint)


# ---------------------------------------------------------------------------
# GET /api/monitoring/metrics  (SPEC-07 S-07.10)
# ---------------------------------------------------------------------------


@router.get(
    "/metrics",
    response_model=AggregatedMetricsResponse,
    summary="Return aggregated LLM usage per agent type",
)
async def get_aggregated_llm_metrics() -> AggregatedMetricsResponse:
    """Return aggregated LLM usage across all runs, grouped by agent type.

    Data is sourced from the in-memory telemetry store (no Redis or DB
    queries). Always returns HTTP 200 — the store may be empty if no
    agent pipeline has executed yet.

    Response includes per-agent totals (tokens in/out, cost, invocation
    count) and a global count of unique candidates processed.

    Log events:
        aggregated_metrics_requested  INFO
        aggregated_metrics_returned   INFO — agent_count, total_candidates
    """
    start = time.monotonic()
    logger.info("aggregated_metrics_requested")

    aggregated = get_aggregated_metrics()

    agents = [
        AgentUsageSummary(
            agent_name=a.agent_name,
            total_tokens_in=a.total_tokens_in,
            total_tokens_out=a.total_tokens_out,
            total_cost=a.total_cost,
            invocation_count=a.invocation_count,
        )
        for a in aggregated
    ]

    # Unique candidates = distinct candidate_ids across all telemetry records.
    # Cannot derive from per-agent aggregation (double-counts candidates
    # processed by multiple agents). Direct store query instead.
    total_candidates = _count_unique_candidates()

    # Response time percentiles from in-memory histogram (SPEC-08 S-08.3).
    snapshot = get_metrics().snapshot()
    rt_hist = snapshot.get("histograms", {}).get("response_duration_ms", None)
    response_time: ResponseTimePercentiles | None = None
    if rt_hist and rt_hist.get("count", 0) > 0:
        response_time = ResponseTimePercentiles(
            p50=round(rt_hist["p50"], 2),
            p95=round(rt_hist["p95"], 2),
            p99=round(rt_hist["p99"], 2),
        )

    logger.info(
        "aggregated_metrics_returned",
        agent_count=len(agents),
        total_candidates=total_candidates,
    )

    _observe_response_time("metrics", start)
    return AggregatedMetricsResponse(
        run_id="",
        status="success",
        agents=agents,
        total_candidates_processed=total_candidates,
        response_time=response_time,
    )


def _count_unique_candidates() -> int:
    """Count unique candidates across all runs in the telemetry store.

    Imports the private telemetry lock/store to iterate candidate_ids
    without copying the full record list.  Thread-safe.
    """
    from api.observability.metrics import _telemetry, _telemetry_lock

    with _telemetry_lock:
        unique = {key[1] for key in _telemetry}  # key = (run_id, candidate_id, agent_name)
    return len(unique)


# ---------------------------------------------------------------------------
# GET /api/monitoring/agents/{run_id}  (SPEC-07 S-07.10)
# ---------------------------------------------------------------------------


@router.get(
    "/agents/{run_id}",
    response_model=AgentTelemetryResponse,
    summary="Return per-candidate agent chain telemetry for a run",
)
async def get_agent_telemetry_for_run(run_id: str) -> AgentTelemetryResponse:
    """Return per-candidate agent chain telemetry for the specified run.

    Data is sourced from the in-memory telemetry store. Returns all
    telemetry records for the given run_id, sorted by (candidate_id,
    agent_name).

    Returns HTTP 200 with an empty list if no telemetry exists for the
    run (fail-open per SKILL-D R-D13).

    Log events:
        agent_telemetry_requested  INFO — run_id
        agent_telemetry_returned   INFO — run_id, record_count
    """
    logger.info("agent_telemetry_requested", run_id=run_id)

    records = get_agent_telemetry(run_id)

    out = [
        AgentTelemetryOut(
            run_id=rec.run_id,
            candidate_id=rec.candidate_id,
            agent_name=rec.agent_name,
            tokens_in=rec.tokens_in,
            tokens_out=rec.tokens_out,
            cost_estimate=rec.cost_estimate,
            execution_time_ms=rec.execution_time_ms,
            evidence_score=rec.evidence_score,
            timestamp=rec.timestamp,
        )
        for rec in records
    ]

    logger.info(
        "agent_telemetry_returned",
        run_id=run_id,
        record_count=len(out),
    )

    return AgentTelemetryResponse(
        run_id=run_id,
        status="success",
        records=out,
        total=len(out),
    )


# ---------------------------------------------------------------------------
# GET /api/monitoring/cache  (SPEC-08 S-08.3)
# ---------------------------------------------------------------------------


@router.get(
    "/cache",
    response_model=CacheStatsResponse,
    summary="Return cache statistics from Redis and application counters",
)
async def get_cache_stats() -> CacheStatsResponse:
    """Return cache statistics combining Redis-level and application-level data.

    Redis-level stats (via INFO and DBSIZE commands):
    - **total_keys**: Number of keys in the current database.
    - **memory_used_bytes / memory_used_human**: Redis memory consumption.

    Application-level counters (from in-memory MetricsCollector):
    - **hit_count / miss_count**: CacheClient.get() outcomes.
    - **hit_ratio**: hit_count / (hit_count + miss_count), 0.0 when no data.

    Always returns HTTP 200. Returns zeros if Redis is unavailable (fail-open
    per SKILL-D R-D9).

    Log events:
        cache_stats_requested  INFO
        cache_stats_returned   INFO — total_keys, hit_count, miss_count, hit_ratio
        cache_stats_redis_unavailable  WARNING — error
    """
    start = time.monotonic()
    logger.info("cache_stats_requested")

    cache = get_cache_client()
    total_keys = 0
    memory_used_bytes = 0
    memory_used_human = "0B"

    # Redis INFO for memory stats.
    redis_info = await cache.info("memory")
    if redis_info:
        memory_used_bytes = redis_info.get("used_memory", 0)
        memory_used_human = redis_info.get("used_memory_human", "0B")

    # DBSIZE for key count.
    total_keys = await cache.dbsize()

    # Application-level hit/miss counters from MetricsCollector.
    snapshot = get_metrics().snapshot()
    counters = snapshot.get("counters", {})
    hit_count = counters.get("cache_hits", 0)
    miss_count = counters.get("cache_misses", 0)
    total = hit_count + miss_count
    hit_ratio = round(hit_count / total, 4) if total > 0 else 0.0

    logger.info(
        "cache_stats_returned",
        total_keys=total_keys,
        hit_count=hit_count,
        miss_count=miss_count,
        hit_ratio=hit_ratio,
    )

    _observe_response_time("cache", start)
    return CacheStatsResponse(
        run_id="",
        status="success",
        total_keys=total_keys,
        memory_used_bytes=memory_used_bytes,
        memory_used_human=memory_used_human,
        hit_count=hit_count,
        miss_count=miss_count,
        hit_ratio=hit_ratio,
    )
