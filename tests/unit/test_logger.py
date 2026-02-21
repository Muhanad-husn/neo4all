"""
tests/unit/test_logger.py — Logger output format and ring buffer (SPEC-01 file 26).

Acceptance criteria (SKILL-D R-D1, R-D3, R-D4, SPEC-01):
  - The ring buffer sink appends every log record to _LOG_BUFFER (newest first).
  - get_recent_logs() returns entries from the buffer with correct ordering.
  - get_recent_logs(level=...) filters by minimum severity rank.
  - configure_logging() is idempotent: the second call is always a no-op.
  - configure_logging(log_format="json") installs a JSON-capable ProcessorFormatter.
  - configure_logging(log_format="console") installs a console formatter.
  - get_logger() returns a named structlog logger.

No network, no LLM, no Neo4j — pure in-process logging machinery.
The _CONFIGURED flag and _LOG_BUFFER are reset via monkeypatch between tests
so each test gets a clean logger state.
"""

import json
import logging

import pytest
import structlog

import api.observability.logger as logger_module
from api.observability.logger import (
    _LEVEL_RANKS,
    _LOG_BUFFER,
    _ring_buffer_sink,
    configure_logging,
    get_logger,
    get_recent_logs,
)

# ---------------------------------------------------------------------------
# Fixture: clean logger state per test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_logger_state(monkeypatch):
    """Reset the logger between tests.

    1. Set _CONFIGURED = False so configure_logging() can be called again.
    2. Clear the ring buffer so previous test entries don't bleed through.
    3. Reset structlog defaults so cached loggers pick up fresh configuration.

    monkeypatch restores _CONFIGURED to its original value after each test,
    but structlog.reset_defaults() and buffer.clear() are handled explicitly.
    """
    monkeypatch.setattr(logger_module, "_CONFIGURED", False)
    _LOG_BUFFER.clear()
    structlog.reset_defaults()
    yield
    _LOG_BUFFER.clear()
    structlog.reset_defaults()


# ---------------------------------------------------------------------------
# Test: _ring_buffer_sink processor
# ---------------------------------------------------------------------------

def test_ring_buffer_sink_is_a_passthrough() -> None:
    """_ring_buffer_sink must return the event_dict unchanged (processor contract)."""
    event_dict = {"event": "test_event", "level": "info", "foo": "bar"}
    result = _ring_buffer_sink(None, "info", event_dict)
    assert result is event_dict


def test_ring_buffer_sink_appends_to_buffer() -> None:
    """_ring_buffer_sink adds a copy of the event dict to _LOG_BUFFER."""
    assert len(_LOG_BUFFER) == 0

    event_dict = {"event": "schema_locked", "level": "info", "run_id": "abc"}
    _ring_buffer_sink(None, "info", event_dict)

    assert len(_LOG_BUFFER) == 1
    assert _LOG_BUFFER[0]["event"] == "schema_locked"


def test_ring_buffer_sink_stores_a_copy() -> None:
    """The buffer stores a snapshot, not a reference to the original dict."""
    event_dict = {"event": "original", "level": "info"}
    _ring_buffer_sink(None, "info", event_dict)

    # Mutate the original after the call.
    event_dict["event"] = "mutated"

    # Buffer entry must still hold the original value.
    assert _LOG_BUFFER[0]["event"] == "original"


def test_ring_buffer_sink_newest_first() -> None:
    """Multiple sink calls result in newest entry at index 0 (deque.appendleft)."""
    _ring_buffer_sink(None, "info", {"event": "first", "level": "info"})
    _ring_buffer_sink(None, "info", {"event": "second", "level": "info"})
    _ring_buffer_sink(None, "info", {"event": "third", "level": "info"})

    assert _LOG_BUFFER[0]["event"] == "third"
    assert _LOG_BUFFER[1]["event"] == "second"
    assert _LOG_BUFFER[2]["event"] == "first"


def test_ring_buffer_respects_maxlen() -> None:
    """_LOG_BUFFER has maxlen=1000; entries beyond the limit are evicted."""
    from api.observability.logger import _MAX_BUFFER
    assert _LOG_BUFFER.maxlen == _MAX_BUFFER

    # Fill to capacity + 1.
    for i in range(_MAX_BUFFER + 1):
        _ring_buffer_sink(None, "info", {"event": f"evt_{i}", "level": "info"})

    assert len(_LOG_BUFFER) == _MAX_BUFFER


# ---------------------------------------------------------------------------
# Test: get_recent_logs()
# ---------------------------------------------------------------------------

def test_get_recent_logs_empty_buffer() -> None:
    """get_recent_logs() returns an empty list when the buffer is empty."""
    assert get_recent_logs() == []


def test_get_recent_logs_returns_all_entries_by_default() -> None:
    """With no level filter and limit > buffer size, all entries are returned."""
    for i in range(5):
        _ring_buffer_sink(None, "info", {"event": f"evt_{i}", "level": "info"})

    results = get_recent_logs(limit=100)
    assert len(results) == 5


def test_get_recent_logs_respects_limit() -> None:
    """get_recent_logs(limit=n) returns at most n entries."""
    for i in range(10):
        _ring_buffer_sink(None, "info", {"event": f"evt_{i}", "level": "info"})

    results = get_recent_logs(limit=3)
    assert len(results) == 3


def test_get_recent_logs_returns_newest_first() -> None:
    """Results are ordered newest-first, consistent with buffer layout."""
    _ring_buffer_sink(None, "info", {"event": "old", "level": "info"})
    _ring_buffer_sink(None, "info", {"event": "new", "level": "info"})

    results = get_recent_logs(limit=10)
    assert results[0]["event"] == "new"
    assert results[1]["event"] == "old"


def test_get_recent_logs_level_filter_warning() -> None:
    """level='WARNING' returns only WARNING, ERROR, and CRITICAL entries."""
    _ring_buffer_sink(None, "debug",    {"event": "debug_evt",    "level": "debug"})
    _ring_buffer_sink(None, "info",     {"event": "info_evt",     "level": "info"})
    _ring_buffer_sink(None, "warning",  {"event": "warning_evt",  "level": "warning"})
    _ring_buffer_sink(None, "error",    {"event": "error_evt",    "level": "error"})
    _ring_buffer_sink(None, "critical", {"event": "critical_evt", "level": "critical"})

    results = get_recent_logs(level="WARNING")
    events = {r["event"] for r in results}

    assert "warning_evt"  in events
    assert "error_evt"    in events
    assert "critical_evt" in events
    assert "debug_evt"    not in events
    assert "info_evt"     not in events


def test_get_recent_logs_level_filter_error() -> None:
    """level='ERROR' returns only ERROR and CRITICAL."""
    _ring_buffer_sink(None, "warning",  {"event": "warn_evt",  "level": "warning"})
    _ring_buffer_sink(None, "error",    {"event": "error_evt", "level": "error"})
    _ring_buffer_sink(None, "critical", {"event": "crit_evt",  "level": "critical"})

    results = get_recent_logs(level="ERROR")
    events = {r["event"] for r in results}

    assert "error_evt"   in events
    assert "crit_evt"    in events
    assert "warn_evt" not in events


def test_get_recent_logs_level_filter_is_case_insensitive() -> None:
    """The level parameter is case-insensitive ('warning' == 'WARNING')."""
    _ring_buffer_sink(None, "warning", {"event": "w_evt", "level": "warning"})
    _ring_buffer_sink(None, "info",    {"event": "i_evt", "level": "info"})

    results_upper = get_recent_logs(level="WARNING")
    results_lower = get_recent_logs(level="warning")

    # Both calls must return the same set of events.
    assert {r["event"] for r in results_upper} == {r["event"] for r in results_lower}


def test_get_recent_logs_no_level_filter_returns_all() -> None:
    """Omitting the level parameter returns entries at all severity levels."""
    for lvl in ("debug", "info", "warning", "error"):
        _ring_buffer_sink(None, lvl, {"event": f"{lvl}_evt", "level": lvl})

    results = get_recent_logs()
    assert len(results) == 4


# ---------------------------------------------------------------------------
# Test: configure_logging() behaviour
# ---------------------------------------------------------------------------

def test_configure_logging_sets_configured_flag() -> None:
    """After configure_logging(), _CONFIGURED is True."""
    assert logger_module._CONFIGURED is False
    configure_logging(log_format="console", log_level="INFO")
    assert logger_module._CONFIGURED is True


def test_configure_logging_is_idempotent() -> None:
    """Calling configure_logging() a second time does not re-run setup.

    We verify this by checking that _CONFIGURED remains True and no
    exception is raised on the second call.
    """
    configure_logging(log_format="json", log_level="INFO")
    assert logger_module._CONFIGURED is True

    # This second call must be a no-op (does not raise, does not reset flag).
    configure_logging(log_format="console", log_level="DEBUG")
    assert logger_module._CONFIGURED is True


def test_configure_logging_installs_stream_handler() -> None:
    """configure_logging() adds a StreamHandler to the stdlib root logger."""
    configure_logging(log_format="console", log_level="INFO")
    root = logging.getLogger()
    stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    assert stream_handlers, "Root logger must have at least one StreamHandler"


def test_configure_logging_sets_log_level() -> None:
    """configure_logging(log_level='DEBUG') sets the root logger level to DEBUG."""
    configure_logging(log_format="console", log_level="DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG


def test_configure_logging_json_format_installs_json_renderer() -> None:
    """configure_logging(log_format='json') installs a JSONRenderer in the
    ProcessorFormatter chain attached to the root logger's StreamHandler.
    """
    configure_logging(log_format="json", log_level="INFO")
    root = logging.getLogger()

    handler = next(
        (h for h in root.handlers if isinstance(h, logging.StreamHandler)), None
    )
    assert handler is not None, "No StreamHandler found on root logger"
    assert isinstance(handler.formatter, structlog.stdlib.ProcessorFormatter), (
        "Handler formatter must be a ProcessorFormatter"
    )

    # The last processor in the formatter chain should be a JSONRenderer.
    procs = handler.formatter.processors
    assert any(
        isinstance(p, structlog.processors.JSONRenderer) for p in procs
    ), "JSONRenderer not found in ProcessorFormatter chain for json format"


def test_configure_logging_console_format_installs_console_renderer() -> None:
    """configure_logging(log_format='console') installs a ConsoleRenderer."""
    configure_logging(log_format="console", log_level="INFO")
    root = logging.getLogger()

    handler = next(
        (h for h in root.handlers if isinstance(h, logging.StreamHandler)), None
    )
    assert handler is not None

    procs = handler.formatter.processors
    assert any(
        isinstance(p, structlog.dev.ConsoleRenderer) for p in procs
    ), "ConsoleRenderer not found in ProcessorFormatter chain for console format"


def test_configure_logging_json_output_is_parseable(capsys) -> None:
    """After configure_logging(log_format='json'), emitted log lines are valid JSON.

    We capture stdout directly and parse the output to assert JSON validity
    and the presence of core structured fields.
    """
    configure_logging(log_format="json", log_level="DEBUG")

    # Get a fresh logger (structlog resets defaults in the fixture).
    logger = get_logger("test.json_format")
    logger.info("json_format_test", run_id="test_run", value=42)

    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.strip().splitlines() if ln.strip()]
    assert lines, "No output was emitted to stdout"

    # The last line must be parseable JSON.
    parsed = json.loads(lines[-1])
    assert parsed.get("event") == "json_format_test"
    assert "level" in parsed
    assert "timestamp" in parsed


# ---------------------------------------------------------------------------
# Test: get_logger()
# ---------------------------------------------------------------------------

def test_get_logger_returns_bound_logger() -> None:
    """get_logger(__name__) returns a structlog BoundLogger (not a stdlib logger)."""
    logger = get_logger("test.component")
    # structlog loggers have .info, .warning, .error, .debug methods.
    assert callable(getattr(logger, "info", None))
    assert callable(getattr(logger, "warning", None))
    assert callable(getattr(logger, "error", None))


def test_level_ranks_cover_all_standard_levels() -> None:
    """_LEVEL_RANKS must include debug, info, warning, error, and critical."""
    required = {"debug", "info", "warning", "error", "critical"}
    assert required.issubset(_LEVEL_RANKS.keys())
