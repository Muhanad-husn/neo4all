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
from typing import Any

_HISTOGRAM_MAXLEN: int = 10_000


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
