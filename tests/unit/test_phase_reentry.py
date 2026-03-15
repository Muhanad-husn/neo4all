"""
tests/unit/test_phase_reentry.py — Phase re-entry mechanism (CURATION → INGESTION).

Acceptance criteria:
  - reenter_phase() allows CURATION→INGESTION and EXTRACTION→INGESTION.
  - reenter_phase() rejects invalid transitions (e.g. SCHEMA→INGESTION).
  - reenter_phase() sets reentry_source to the current phase.
  - clear_reentry() resets reentry_source to None.
  - reset() clears reentry_source.
  - restore_session() restores reentry_source from persisted state.

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
# Test: reenter_phase — valid transitions
# ---------------------------------------------------------------------------


def test_reenter_curation_to_ingestion() -> None:
    """CURATION → INGESTION is a valid re-entry path."""
    state = _advance_to(Phase.CURATION)
    state.reenter_phase(Phase.INGESTION)
    assert state.phase == Phase.INGESTION
    assert state.reentry_source == Phase.CURATION


def test_reenter_extraction_to_ingestion() -> None:
    """EXTRACTION → INGESTION is a valid re-entry path."""
    state = _advance_to(Phase.EXTRACTION)
    state.reenter_phase(Phase.INGESTION)
    assert state.phase == Phase.INGESTION
    assert state.reentry_source == Phase.EXTRACTION


# ---------------------------------------------------------------------------
# Test: reenter_phase — rejected transitions
# ---------------------------------------------------------------------------


def test_reenter_schema_to_ingestion_rejected() -> None:
    """SCHEMA → INGESTION is NOT a valid re-entry path."""
    state = _advance_to(Phase.SCHEMA)
    with pytest.raises(ValueError, match="Re-entry rejected"):
        state.reenter_phase(Phase.INGESTION)
    assert state.phase == Phase.SCHEMA


def test_reenter_ingestion_to_ingestion_rejected() -> None:
    """INGESTION → INGESTION (same phase) is NOT a valid re-entry path."""
    state = _advance_to(Phase.INGESTION)
    with pytest.raises(ValueError, match="Re-entry rejected"):
        state.reenter_phase(Phase.INGESTION)


def test_reenter_curation_to_schema_rejected() -> None:
    """CURATION → SCHEMA is NOT a valid re-entry path."""
    state = _advance_to(Phase.CURATION)
    with pytest.raises(ValueError, match="Re-entry rejected"):
        state.reenter_phase(Phase.SCHEMA)


# ---------------------------------------------------------------------------
# Test: reenter_phase — phase timing recorded
# ---------------------------------------------------------------------------


def test_reenter_records_phase_timing() -> None:
    """Re-entry records a new phase start time for the target phase."""
    state = _advance_to(Phase.CURATION)
    timings_before = dict(state.phase_start_times)
    ingestion_time_before = timings_before.get(Phase.INGESTION.value)

    state.reenter_phase(Phase.INGESTION)

    ingestion_time_after = state.phase_start_times[Phase.INGESTION.value]
    # The timing should be updated (different from the original).
    assert ingestion_time_after != ingestion_time_before


# ---------------------------------------------------------------------------
# Test: clear_reentry
# ---------------------------------------------------------------------------


def test_clear_reentry() -> None:
    """clear_reentry() sets reentry_source back to None."""
    state = _advance_to(Phase.CURATION)
    state.reenter_phase(Phase.INGESTION)
    assert state.reentry_source is not None

    state.clear_reentry()
    assert state.reentry_source is None


# ---------------------------------------------------------------------------
# Test: reentry_source default
# ---------------------------------------------------------------------------


def test_reentry_source_default_is_none() -> None:
    """reentry_source defaults to None on a fresh session."""
    state = StateManager.get()
    assert state.reentry_source is None


# ---------------------------------------------------------------------------
# Test: reset clears reentry
# ---------------------------------------------------------------------------


def test_reset_clears_reentry_source() -> None:
    """reset() clears reentry_source along with all other state."""
    state = _advance_to(Phase.CURATION)
    state.reenter_phase(Phase.INGESTION)
    assert state.reentry_source is not None

    state.reset()
    assert state.reentry_source is None


# ---------------------------------------------------------------------------
# Test: restore_session with reentry_source
# ---------------------------------------------------------------------------


def test_restore_session_with_reentry_source() -> None:
    """restore_session() restores reentry_source from persisted state."""
    state = StateManager.get()
    state.restore_session(
        run_id="abc123" * 10 + "abcd",
        timestamp_seed="2026-01-01T00:00:00+00:00",
        phase=Phase.INGESTION.value,
        schema_version="v1",
        neo4j_uri="neo4j+s://test.databases.neo4j.io",
        neo4j_user="neo4j",
        neo4j_password="secret",
        openrouter_api_key="sk-or-test",
        reentry_source=Phase.CURATION.value,
    )
    assert state.reentry_source == Phase.CURATION
    assert state.phase == Phase.INGESTION


def test_restore_session_without_reentry_source() -> None:
    """restore_session() defaults reentry_source to None when not provided."""
    state = StateManager.get()
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
    assert state.reentry_source is None


# ---------------------------------------------------------------------------
# Test: advance_phase still works after re-entry
# ---------------------------------------------------------------------------


def test_advance_after_reentry() -> None:
    """After re-entering INGESTION, advance_phase(EXTRACTION) works."""
    state = _advance_to(Phase.CURATION)
    state.reenter_phase(Phase.INGESTION)
    assert state.phase == Phase.INGESTION

    state.advance_phase(Phase.EXTRACTION)
    assert state.phase == Phase.EXTRACTION

    state.clear_reentry()
    state.advance_phase(Phase.CURATION)
    assert state.phase == Phase.CURATION
    assert state.reentry_source is None
