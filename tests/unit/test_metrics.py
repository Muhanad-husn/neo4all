"""
tests/unit/test_metrics.py — MetricsCollector correctness (SPEC-01 file 27).

Acceptance criteria (SKILL-D R-D13, SPEC-01):
  - increment() adds value to a named counter (default value=1).
  - increment() with a custom value accumulates correctly.
  - increment() with labels produces a distinct internal key per label set.
  - gauge() sets the absolute value for a metric; subsequent calls overwrite.
  - observe() appends values to a named histogram.
  - _Histogram.summary() returns zeros for an empty histogram.
  - _Histogram.summary() returns correct count, sum, min, max, mean for
    a known set of observations.
  - snapshot() returns a dict with 'counters', 'gauges', and 'histograms' keys.
  - snapshot() reflects all mutations recorded before the call.
  - reset() clears counters, gauges, and histograms completely.
  - get_metrics() always returns the same process-level singleton.
  - _label_key() produces sorted, stable keys regardless of kwarg insertion order.

No network, no LLM, no Neo4j — pure in-process metrics machinery.
reset() is called before and after every test via autouse fixture.
"""

import threading

import pytest

from api.observability.metrics import (
    _label_key,
    get_metrics,
)

# ---------------------------------------------------------------------------
# Fixture: reset metrics state per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_metrics():
    """Ensure a clean metrics slate for every test.

    reset() before the test guards against state leaked from module-level
    imports or other tests; reset() after (via yield) ensures the process
    singleton is clean for subsequent tests.
    """
    get_metrics().reset()
    yield
    get_metrics().reset()


# ---------------------------------------------------------------------------
# Test: _label_key encoding
# ---------------------------------------------------------------------------

def test_label_key_no_labels() -> None:
    """With no labels, _label_key returns the bare metric name."""
    assert _label_key("my_metric", {}) == "my_metric"


def test_label_key_single_label() -> None:
    """A single label is formatted as name{key=value}."""
    key = _label_key("req_count", {"status": "ok"})
    assert key == "req_count{status=ok}"


def test_label_key_multiple_labels_sorted() -> None:
    """Multiple labels are sorted alphabetically by key name."""
    key = _label_key("req_count", {"status": "ok", "agent": "agent-a"})
    # 'agent' sorts before 'status'
    assert key == "req_count{agent=agent-a,status=ok}"


def test_label_key_insertion_order_does_not_matter() -> None:
    """_label_key produces the same output regardless of dict insertion order."""
    key_a = _label_key("m", {"z": "1", "a": "2"})
    key_b = _label_key("m", {"a": "2", "z": "1"})
    assert key_a == key_b


# ---------------------------------------------------------------------------
# Test: increment()
# ---------------------------------------------------------------------------

def test_increment_default_value() -> None:
    """increment() with no value argument increments by 1."""
    m = get_metrics()
    m.increment("docs_ingested")
    snap = m.snapshot()
    assert snap["counters"]["docs_ingested"] == 1


def test_increment_custom_value() -> None:
    """increment(value=5) increments the counter by 5."""
    m = get_metrics()
    m.increment("docs_ingested", value=5)
    snap = m.snapshot()
    assert snap["counters"]["docs_ingested"] == 5


def test_increment_accumulates() -> None:
    """Multiple increment() calls accumulate (are not idempotent overwrites)."""
    m = get_metrics()
    m.increment("docs_ingested")
    m.increment("docs_ingested")
    m.increment("docs_ingested", value=3)
    snap = m.snapshot()
    assert snap["counters"]["docs_ingested"] == 5


def test_increment_with_labels_produces_distinct_key() -> None:
    """Labeled increments are tracked independently from the unlabeled counter."""
    m = get_metrics()
    m.increment("llm_requests", agent="agent-a", status="ok")
    m.increment("llm_requests", agent="agent-b", status="error")
    m.increment("llm_requests")  # unlabeled

    snap = m.snapshot()
    counters = snap["counters"]

    assert counters["llm_requests{agent=agent-a,status=ok}"] == 1
    assert counters["llm_requests{agent=agent-b,status=error}"] == 1
    assert counters["llm_requests"] == 1


def test_increment_labeled_accumulates_independently() -> None:
    """Increments to different label sets do not affect each other."""
    m = get_metrics()
    m.increment("jobs", status="ok")
    m.increment("jobs", status="ok")
    m.increment("jobs", status="error")

    snap = m.snapshot()
    assert snap["counters"]["jobs{status=ok}"] == 2
    assert snap["counters"]["jobs{status=error}"] == 1


def test_increment_multiple_distinct_counters() -> None:
    """Two different metric names remain independent."""
    m = get_metrics()
    m.increment("metric_a")
    m.increment("metric_b")
    m.increment("metric_b")

    snap = m.snapshot()
    assert snap["counters"]["metric_a"] == 1
    assert snap["counters"]["metric_b"] == 2


# ---------------------------------------------------------------------------
# Test: gauge()
# ---------------------------------------------------------------------------

def test_gauge_set_value() -> None:
    """gauge() stores the given value for retrieval via snapshot()."""
    m = get_metrics()
    m.gauge("queue_depth", 42.0)
    snap = m.snapshot()
    assert snap["gauges"]["queue_depth"] == 42.0


def test_gauge_overwrites_previous_value() -> None:
    """A second gauge() call replaces (not adds to) the stored value."""
    m = get_metrics()
    m.gauge("queue_depth", 10.0)
    m.gauge("queue_depth", 3.0)
    snap = m.snapshot()
    assert snap["gauges"]["queue_depth"] == 3.0


def test_gauge_with_labels() -> None:
    """Labeled gauges are stored under their encoded key."""
    m = get_metrics()
    m.gauge("active_jobs", 7.0, worker="w1")
    snap = m.snapshot()
    assert snap["gauges"]["active_jobs{worker=w1}"] == 7.0


def test_gauge_fractional_value() -> None:
    """gauge() stores fractional float values without truncation."""
    m = get_metrics()
    m.gauge("cpu_usage", 0.753)
    snap = m.snapshot()
    assert abs(snap["gauges"]["cpu_usage"] - 0.753) < 1e-9


# ---------------------------------------------------------------------------
# Test: observe() / histogram
# ---------------------------------------------------------------------------

def test_observe_populates_histogram() -> None:
    """observe() creates a histogram entry visible in snapshot()."""
    m = get_metrics()
    m.observe("request_duration_ms", 120.5)
    snap = m.snapshot()
    assert "request_duration_ms" in snap["histograms"]


def test_histogram_empty_summary_returns_zeros() -> None:
    """A histogram that has never been observed returns all-zero summary fields."""
    # We access the internal _Histogram class to test an empty histogram.
    from api.observability.metrics import _Histogram
    h = _Histogram()
    s = h.summary()
    assert s["count"] == 0.0
    assert s["sum"] == 0.0
    assert s["min"] == 0.0
    assert s["max"] == 0.0
    assert s["mean"] == 0.0
    assert s["p95"] == 0.0
    assert s["p99"] == 0.0


def test_histogram_summary_single_value() -> None:
    """A histogram with one observation reports consistent summary statistics."""
    m = get_metrics()
    m.observe("latency", 50.0)
    snap = m.snapshot()
    s = snap["histograms"]["latency"]

    assert s["count"] == 1.0
    assert s["sum"] == 50.0
    assert s["min"] == 50.0
    assert s["max"] == 50.0
    assert s["mean"] == 50.0
    # p95 and p99 of a single-element distribution must be that element.
    assert s["p95"] == 50.0
    assert s["p99"] == 50.0


def test_histogram_summary_multiple_values() -> None:
    """summary() computes correct count, sum, min, max, mean for a known dataset."""
    m = get_metrics()
    for v in [10.0, 20.0, 30.0, 40.0, 50.0]:
        m.observe("latency", v)

    snap = m.snapshot()
    s = snap["histograms"]["latency"]

    assert s["count"] == 5.0
    assert abs(s["sum"] - 150.0) < 1e-9
    assert s["min"] == 10.0
    assert s["max"] == 50.0
    assert abs(s["mean"] - 30.0) < 1e-9


def test_histogram_accumulates_observations() -> None:
    """Multiple observe() calls on the same name add to the same histogram."""
    m = get_metrics()
    m.observe("latency", 1.0)
    m.observe("latency", 2.0)
    m.observe("latency", 3.0)

    snap = m.snapshot()
    assert snap["histograms"]["latency"]["count"] == 3.0


def test_histogram_maxlen_is_respected() -> None:
    """Observations beyond maxlen=10_000 evict the oldest entries."""
    from api.observability.metrics import _HISTOGRAM_MAXLEN, _Histogram

    h = _Histogram()
    for i in range(_HISTOGRAM_MAXLEN + 10):
        h.observe(float(i))

    assert h.summary()["count"] == float(_HISTOGRAM_MAXLEN)


def test_observe_with_labels() -> None:
    """Labeled observe() calls use the encoded key in the snapshot."""
    m = get_metrics()
    m.observe("duration_ms", 99.0, route="/api/health")
    snap = m.snapshot()
    assert "duration_ms{route=/api/health}" in snap["histograms"]


# ---------------------------------------------------------------------------
# Test: snapshot()
# ---------------------------------------------------------------------------

def test_snapshot_returns_three_categories() -> None:
    """snapshot() always returns a dict with counters, gauges, histograms keys."""
    snap = get_metrics().snapshot()
    assert "counters" in snap
    assert "gauges" in snap
    assert "histograms" in snap


def test_snapshot_is_a_copy() -> None:
    """Mutating the snapshot dict does not affect the live metrics store."""
    m = get_metrics()
    m.increment("count_a")
    snap = m.snapshot()
    snap["counters"]["count_a"] = 999  # mutate the copy

    # The live store must be unaffected.
    live_snap = m.snapshot()
    assert live_snap["counters"]["count_a"] == 1


def test_snapshot_reflects_all_metric_types() -> None:
    """A snapshot taken after mixed operations contains all three metric types."""
    m = get_metrics()
    m.increment("events")
    m.gauge("queue", 5.0)
    m.observe("latency", 10.0)

    snap = m.snapshot()
    assert "events" in snap["counters"]
    assert "queue" in snap["gauges"]
    assert "latency" in snap["histograms"]


def test_snapshot_empty_store_returns_empty_dicts() -> None:
    """After reset(), snapshot() returns dicts with no entries."""
    m = get_metrics()
    snap = m.snapshot()
    assert snap["counters"] == {}
    assert snap["gauges"] == {}
    assert snap["histograms"] == {}


# ---------------------------------------------------------------------------
# Test: reset()
# ---------------------------------------------------------------------------

def test_reset_clears_counters() -> None:
    """reset() removes all counter entries."""
    m = get_metrics()
    m.increment("x")
    m.reset()
    assert m.snapshot()["counters"] == {}


def test_reset_clears_gauges() -> None:
    """reset() removes all gauge entries."""
    m = get_metrics()
    m.gauge("x", 1.0)
    m.reset()
    assert m.snapshot()["gauges"] == {}


def test_reset_clears_histograms() -> None:
    """reset() removes all histogram entries."""
    m = get_metrics()
    m.observe("x", 1.0)
    m.reset()
    assert m.snapshot()["histograms"] == {}


def test_reset_allows_fresh_accumulation() -> None:
    """After reset(), increment() starts from zero again."""
    m = get_metrics()
    m.increment("count", value=10)
    m.reset()
    m.increment("count", value=3)
    assert m.snapshot()["counters"]["count"] == 3


# ---------------------------------------------------------------------------
# Test: get_metrics() singleton
# ---------------------------------------------------------------------------

def test_get_metrics_returns_same_instance() -> None:
    """get_metrics() always returns the identical object (process singleton)."""
    assert get_metrics() is get_metrics()


def test_get_metrics_state_shared_across_calls() -> None:
    """Mutations via one get_metrics() reference are visible via another."""
    get_metrics().increment("shared_counter")
    snap = get_metrics().snapshot()
    assert snap["counters"]["shared_counter"] == 1


def test_get_metrics_is_not_a_new_collector() -> None:
    """get_metrics() does not construct a new MetricsCollector each time."""
    m1 = get_metrics()
    m2 = get_metrics()
    assert m1 is m2


# ---------------------------------------------------------------------------
# Test: thread safety
# ---------------------------------------------------------------------------

def test_concurrent_increments_are_thread_safe() -> None:
    """Concurrent increment() calls from multiple threads produce a correct total.

    100 threads each increment the same counter once. The final value must
    equal 100 — no increments lost due to race conditions.
    """
    m = get_metrics()
    thread_count = 100

    def _worker() -> None:
        m.increment("thread_counter")

    threads = [threading.Thread(target=_worker) for _ in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = m.snapshot()
    assert snap["counters"]["thread_counter"] == thread_count
