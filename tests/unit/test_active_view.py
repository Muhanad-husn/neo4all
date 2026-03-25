"""
tests/unit/test_active_view.py — Free navigation via active_view.

Acceptance criteria:
  - active_view defaults to None (show watermark phase).
  - set_active_view() stores any valid view string.
  - set_active_view(None) returns to watermark phase.
  - reset() clears active_view to None.
  - restore_session() does not set active_view (ephemeral).
  - advance_phase() does not change active_view.

st.session_state is replaced by a plain dict for every test so that
StateManager methods can be exercised without a running Streamlit server.
No network, no LLM, no Neo4j — pure in-process state machine.
"""

import pytest

import ui.state as state_module
from api.models.run import Phase
from ui.state import StateManager


@pytest.fixture(autouse=True)
def session_state(monkeypatch):
    """Provide a fresh dict as st.session_state for each test."""
    from unittest.mock import MagicMock

    state: dict = {}
    mock_st = MagicMock()
    mock_st.session_state = state
    monkeypatch.setattr(state_module, "st", mock_st)
    return state


def _advance_to(phase: Phase) -> StateManager:
    """Bootstrap and advance to the given phase."""
    state = StateManager.get()
    for p in Phase:
        if p.value == 0:
            continue
        if p.value <= phase.value:
            state.advance_phase(p)
    return state


# ---------------------------------------------------------------------------
# Test: active_view default
# ---------------------------------------------------------------------------


def test_active_view_default_is_none() -> None:
    """active_view defaults to None on a fresh session."""
    state = StateManager.get()
    assert state.active_view is None


# ---------------------------------------------------------------------------
# Test: set_active_view
# ---------------------------------------------------------------------------


def test_set_active_view_phase() -> None:
    """set_active_view stores a phase view string."""
    state = StateManager.get()
    state.set_active_view("phase:2")
    assert state.active_view == "phase:2"


def test_set_active_view_dashboard() -> None:
    """set_active_view stores 'dashboard'."""
    state = StateManager.get()
    state.set_active_view("dashboard")
    assert state.active_view == "dashboard"


def test_set_active_view_graph_explorer() -> None:
    """set_active_view stores 'graph_explorer'."""
    state = StateManager.get()
    state.set_active_view("graph_explorer")
    assert state.active_view == "graph_explorer"


def test_set_active_view_none() -> None:
    """set_active_view(None) returns to watermark phase."""
    state = StateManager.get()
    state.set_active_view("phase:3")
    assert state.active_view == "phase:3"

    state.set_active_view(None)
    assert state.active_view is None


# ---------------------------------------------------------------------------
# Test: reset clears active_view
# ---------------------------------------------------------------------------


def test_reset_clears_active_view() -> None:
    """reset() clears active_view to None."""
    state = _advance_to(Phase.CURATION)
    state.set_active_view("phase:1")
    assert state.active_view == "phase:1"

    state.reset()
    assert state.active_view is None


# ---------------------------------------------------------------------------
# Test: advance_phase does not change active_view
# ---------------------------------------------------------------------------


def test_advance_phase_preserves_active_view() -> None:
    """advance_phase() does not change active_view."""
    state = StateManager.get()
    state.set_active_view("dashboard")
    state.advance_phase(Phase.SCHEMA)
    assert state.active_view == "dashboard"
    assert state.phase == Phase.SCHEMA


# ---------------------------------------------------------------------------
# Test: restore_session does not set active_view
# ---------------------------------------------------------------------------


def test_restore_session_does_not_set_active_view() -> None:
    """restore_session() leaves active_view at its default (None)."""
    state = StateManager.get()
    state.set_active_view("phase:2")

    state.restore_session(
        run_id="abc123" * 10 + "abcd",
        timestamp_seed="2026-01-01T00:00:00+00:00",
        phase=Phase.CURATION.value,
        schema_version="v1",
        neo4j_uri="neo4j+s://test.databases.neo4j.io",
        neo4j_user="neo4j",
        neo4j_password="secret",
        openrouter_api_key="sk-or-test",
    )
    # restore_session does not touch active_view — it remains as set before
    assert state.active_view == "phase:2"
