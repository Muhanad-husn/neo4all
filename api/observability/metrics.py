"""
api/observability/metrics.py — Thread-safe in-memory metrics collector.

No external dependencies. Monitoring endpoints read snapshots of this store via
get_metrics().snapshot() — no database queries (SKILL-D R-D13).

Metric types
------------
  counter   — Monotonically increasing integer. E.g. documents_ingested.
  gauge     — Floating-point current value. E.g. queue_depth, active_jobs.
  histogram — Distribution of observed values (latency, sizes). Provides
              count, sum, min, max, mean, p95, p99 from bounded window.

Label encoding
--------------
  Metrics may carry string labels that partition the metric. Labels are
  incorporated into the internal key as "{name}{k1=v1,k2=v2}" (sorted).
  This means each unique label combination is tracked independently.

  Example:
      metrics.increment("llm_requests", agent="agent-a", status="ok")
      metrics.increment("llm_requests", agent="agent-b", status="error")

Thread safety
-------------
A single threading.Lock guards all mutation and snapshot operations. This is
sufficient because metrics operations are short (no I/O) and the lock is
never held across yield points.

Histogram memory
----------------
Each histogram is backed by a collections.deque with maxlen=10_000. Older
observations are evicted automatically. Percentiles are computed over the
retained window. For high-throughput counters, prefer increment() over
histogram observe().
"""

import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

_HISTOGRAM_MAXLEN: int = 10_000


# ---------------------------------------------------------------------------
# Agent Telemetry (SPEC-07 S-07.10)
# ---------------------------------------------------------------------------


class AgentTelemetryRecord(BaseModel):
    """Immutable telemetry record for a single agent execution.

    Keyed by (run_id, candidate_id, agent_name) in the telemetry store.
    Recorded after each agent completes (success or failure).

    Attributes:
        run_id:            Governed run context.
        candidate_id:      Candidate being processed by the agent chain.
        agent_name:        Agent identifier (orchestrator, agent-a, agent-b, agent-p).
        tokens_in:         Prompt tokens consumed by the LLM call.
        tokens_out:        Completion tokens produced by the LLM call.
        cost_estimate:     Estimated USD cost of the LLM call.
        execution_time_ms: Wall-clock execution time in milliseconds.
        evidence_score:    Evidence sufficiency score (Agent-A only; None for others).
        timestamp:         ISO 8601 UTC timestamp of record creation.
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

    model_config = {"frozen": True}


class _Histogram:
    """Fixed-capacity circular buffer of float observations."""

    __slots__ = ("_values",)

    def __init__(self) -> None:
        self._values: deque[float] = deque(maxlen=_HISTOGRAM_MAXLEN)

    def observe(self, value: float) -> None:
        self._values.append(value)

    def summary(self) -> dict[str, float]:
        if not self._values:
            return {
                "count": 0.0,
                "sum": 0.0,
                "min": 0.0,
                "max": 0.0,
                "mean": 0.0,
                "p50": 0.0,
                "p95": 0.0,
                "p99": 0.0,
            }
        sv = sorted(self._values)
        n = len(sv)
        return {
            "count": float(n),
            "sum": float(sum(sv)),
            "min": sv[0],
            "max": sv[-1],
            "mean": sum(sv) / n,
            # Index clamped to [0, n-1] so single-element histograms work.
            "p50": sv[max(0, int(n * 0.50) - 1)],
            "p95": sv[max(0, int(n * 0.95) - 1)],
            "p99": sv[max(0, int(n * 0.99) - 1)],
        }


def _label_key(name: str, labels: dict[str, Any]) -> str:
    """Encode name + labels into a stable string key."""
    if not labels:
        return name
    parts = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{parts}}}"


class MetricsCollector:
    """Thread-safe in-memory store for counters, gauges, and histograms."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, _Histogram] = {}

    # ------------------------------------------------------------------
    # Mutation methods
    # ------------------------------------------------------------------

    def increment(self, name: str, value: int = 1, **labels: Any) -> None:
        """Increment a named counter.

        Args:
            name:   Metric name (snake_case, e.g. "documents_ingested").
            value:  Amount to add (default 1).
            labels: Optional keyword label dimensions.
        """
        key = _label_key(name, labels)
        with self._lock:
            self._counters[key] += value

    def gauge(self, name: str, value: float, **labels: Any) -> None:
        """Set a gauge to an absolute value.

        Args:
            name:  Metric name.
            value: Current value.
            labels: Optional keyword label dimensions.
        """
        key = _label_key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def observe(self, name: str, value: float, **labels: Any) -> None:
        """Record an observation in a histogram.

        Typical use: latency in milliseconds, token counts, chunk sizes.

        Args:
            name:  Metric name (e.g. "request_duration_ms").
            value: Observed value.
            labels: Optional keyword label dimensions.
        """
        key = _label_key(name, labels)
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = _Histogram()
            self._histograms[key].observe(value)

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return a point-in-time copy of all metrics.

        Returns:
            Dict with keys "counters", "gauges", "histograms".
            Safe to serialise to JSON.
        """
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {
                    k: hist.summary() for k, hist in self._histograms.items()
                },
            }

    def reset(self) -> None:
        """Clear all metrics. Intended for use in tests only."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


# ---------------------------------------------------------------------------
# Process-level singleton
# ---------------------------------------------------------------------------
_COLLECTOR: MetricsCollector = MetricsCollector()


def get_metrics() -> MetricsCollector:
    """Return the process-level MetricsCollector singleton."""
    return _COLLECTOR


# ---------------------------------------------------------------------------
# Agent Telemetry Store (SPEC-07 S-07.10)
# ---------------------------------------------------------------------------
# In-memory, thread-safe store for per-agent execution telemetry.
# Keyed by (run_id, candidate_id, agent_name) — one record per agent
# invocation.  Overwrites on re-execution of the same agent for the same
# candidate (idempotent).
# ---------------------------------------------------------------------------

_telemetry_lock = threading.Lock()
_telemetry: dict[tuple[str, str, str], AgentTelemetryRecord] = {}


def record_agent_telemetry(
    *,
    run_id: str,
    candidate_id: str,
    agent_name: str,
    tokens_in: int,
    tokens_out: int,
    cost_estimate: float,
    execution_time_ms: float,
    evidence_score: float | None = None,
) -> AgentTelemetryRecord:
    """Store a telemetry record for a single agent execution.

    Args:
        run_id:            Governed run context.
        candidate_id:      Candidate being processed.
        agent_name:        Agent identifier.
        tokens_in:         Prompt tokens consumed.
        tokens_out:        Completion tokens produced.
        cost_estimate:     Estimated USD cost.
        execution_time_ms: Wall-clock time in ms.
        evidence_score:    Evidence sufficiency (Agent-A only).

    Returns:
        The persisted AgentTelemetryRecord.
    """
    record = AgentTelemetryRecord(
        run_id=run_id,
        candidate_id=candidate_id,
        agent_name=agent_name,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_estimate=cost_estimate,
        execution_time_ms=execution_time_ms,
        evidence_score=evidence_score,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    key = (run_id, candidate_id, agent_name)
    with _telemetry_lock:
        _telemetry[key] = record
    return record


def get_agent_telemetry(run_id: str) -> list[AgentTelemetryRecord]:
    """Return all telemetry records for a given run.

    Records are returned sorted by (candidate_id, agent_name) for
    deterministic ordering in API responses.

    Args:
        run_id: The governed run to query.

    Returns:
        List of AgentTelemetryRecord for all candidates in the run.
        Empty list if no telemetry has been recorded for this run.
    """
    with _telemetry_lock:
        records = [
            rec for key, rec in _telemetry.items() if key[0] == run_id
        ]
    records.sort(key=lambda r: (r.candidate_id, r.agent_name))
    return records


class AggregatedAgentMetrics(BaseModel):
    """Aggregated LLM usage across all runs, grouped by agent type.

    Attributes:
        agent_name:      Agent identifier.
        total_tokens_in:  Sum of prompt tokens across all invocations.
        total_tokens_out: Sum of completion tokens across all invocations.
        total_cost:       Sum of cost_estimate across all invocations.
        invocation_count: Number of invocations recorded.
    """

    agent_name: str
    total_tokens_in: int
    total_tokens_out: int
    total_cost: float
    invocation_count: int

    model_config = {"frozen": True}


def get_aggregated_metrics() -> list[AggregatedAgentMetrics]:
    """Return aggregated LLM usage metrics grouped by agent type.

    Iterates the full telemetry store and sums tokens, cost, and counts
    per agent_name.  Returned sorted by agent_name for stable ordering.

    Returns:
        List of AggregatedAgentMetrics, one per distinct agent_name.
        Empty list if no telemetry has been recorded.
    """
    with _telemetry_lock:
        all_records = list(_telemetry.values())

    accum: dict[str, dict[str, Any]] = {}
    for rec in all_records:
        bucket = accum.setdefault(
            rec.agent_name,
            {
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_cost": 0.0,
                "invocation_count": 0,
            },
        )
        bucket["total_tokens_in"] += rec.tokens_in
        bucket["total_tokens_out"] += rec.tokens_out
        bucket["total_cost"] += rec.cost_estimate
        bucket["invocation_count"] += 1

    results = [
        AggregatedAgentMetrics(agent_name=name, **vals)
        for name, vals in sorted(accum.items())
    ]
    return results


def reset_agent_telemetry() -> None:
    """Clear all agent telemetry records. Intended for use in tests only."""
    with _telemetry_lock:
        _telemetry.clear()
