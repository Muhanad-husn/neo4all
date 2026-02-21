"""
tests/unit/test_state_manager.py — StateManager phase enforcement (SPEC-01 file 24).

Acceptance criteria (CLAUDE.md §8, SPEC-01):
  - advance_phase() accepts only the immediately next phase (current + 1).
  - Skipping phases, going backwards, or repeating the current phase raises ValueError.
  - Phase start times are recorded in the session state on every transition.
  - _bootstrap() seeds defaults without overwriting existing keys.
  - initialize_session() derives a deterministic run_id from credentials.
  - Panel modes default to "write"; invalid modes are rejected.

st.session_state is replaced by a plain dict for every test so that
StateManager methods can be exercised without a running Streamlit server.
No network, no LLM, no Neo4j — pure in-process state machine.
"""

import pytest

import ui.state as state_module
from api.models.run import Phase
from ui.state import PANEL_READ, PANEL_WRITE, StateManager

# ---------------------------------------------------------------------------
# Fixture: replace st.session_state with a plain dict
#
# We patch `st` inside the `ui.state` module namespace. This is more
# reliable than patching `streamlit.session_state` directly because it
# avoids Streamlit's module-level __getattr__ machinery.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def session_state(monkeypatch):
    """Provide a fresh dict as st.session_state for each test.

    autouse=True means every test in this module starts with a clean state
    without having to request the fixture explicitly.
    """
    from unittest.mock import MagicMock

    state: dict = {}
    mock_st = MagicMock()
    mock_st.session_state = state
    monkeypatch.setattr(state_module, "st", mock_st)
    return state


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _get_state() -> StateManager:
    """Bootstrap and return a fresh StateManager bound to the patched session."""
    return StateManager.get()


# ---------------------------------------------------------------------------
# Test: phase ordering — accepted transitions
# ---------------------------------------------------------------------------

def test_advance_init_to_schema() -> None:
    """INIT → SCHEMA is the valid first transition."""
    state = _get_state()
    state.advance_phase(Phase.SCHEMA)
    assert state.phase == Phase.SCHEMA


def test_advance_schema_to_ingestion() -> None:
    """SCHEMA → INGESTION is valid."""
    state = _get_state()
    state.advance_phase(Phase.SCHEMA)
    state.advance_phase(Phase.INGESTION)
    assert state.phase == Phase.INGESTION


def test_advance_ingestion_to_extraction() -> None:
    """INGESTION → EXTRACTION is valid."""
    state = _get_state()
    state.advance_phase(Phase.SCHEMA)
    state.advance_phase(Phase.INGESTION)
    state.advance_phase(Phase.EXTRACTION)
    assert state.phase == Phase.EXTRACTION


def test_advance_extraction_to_curation() -> None:
    """EXTRACTION → CURATION completes the full ordered sequence."""
    state = _get_state()
    state.advance_phase(Phase.SCHEMA)
    state.advance_phase(Phase.INGESTION)
    state.advance_phase(Phase.EXTRACTION)
    state.advance_phase(Phase.CURATION)
    assert state.phase == Phase.CURATION


# ---------------------------------------------------------------------------
# Test: phase ordering — rejected transitions
# ---------------------------------------------------------------------------

def test_skip_phase_raises_value_error() -> None:
    """Advancing from INIT directly to INGESTION (skipping SCHEMA) is rejected."""
    state = _get_state()
    assert state.phase == Phase.INIT

    with pytest.raises(ValueError, match="Phase transition rejected"):
        state.advance_phase(Phase.INGESTION)

    # Phase must remain unchanged after a rejected transition.
    assert state.phase == Phase.INIT


def test_skip_two_phases_raises_value_error() -> None:
    """Advancing from INIT to EXTRACTION (skipping two phases) is rejected."""
    state = _get_state()
    with pytest.raises(ValueError):
        state.advance_phase(Phase.EXTRACTION)


def test_backward_transition_raises_value_error() -> None:
    """Going backwards (SCHEMA → INIT) is rejected."""
    state = _get_state()
    state.advance_phase(Phase.SCHEMA)
    assert state.phase == Phase.SCHEMA

    with pytest.raises(ValueError, match="Phase transition rejected"):
        state.advance_phase(Phase.INIT)

    assert state.phase == Phase.SCHEMA


def test_same_phase_transition_raises_value_error() -> None:
    """Advancing to the current phase (INGESTION → INGESTION) is rejected."""
    state = _get_state()
    state.advance_phase(Phase.SCHEMA)
    state.advance_phase(Phase.INGESTION)

    with pytest.raises(ValueError, match="Phase transition rejected"):
        state.advance_phase(Phase.INGESTION)

    assert state.phase == Phase.INGESTION


def test_rejected_transition_does_not_corrupt_state() -> None:
    """A rejected advance_phase() leaves the phase unchanged and does not
    record a spurious timing entry for the rejected target phase."""
    state = _get_state()
    state.advance_phase(Phase.SCHEMA)

    with pytest.raises(ValueError):
        state.advance_phase(Phase.EXTRACTION)  # skip INGESTION

    assert state.phase == Phase.SCHEMA
    # EXTRACTION must not appear in phase timings after a rejected transition.
    timings = state.phase_start_times
    assert Phase.EXTRACTION.value not in timings


# ---------------------------------------------------------------------------
# Test: phase timing
# ---------------------------------------------------------------------------

def test_advance_phase_records_start_time() -> None:
    """A successful advance_phase() adds a timing entry for the new phase."""
    state = _get_state()
    timings_before = dict(state.phase_start_times)

    state.advance_phase(Phase.SCHEMA)

    timings_after = state.phase_start_times
    assert Phase.SCHEMA.value in timings_after
    # SCHEMA timing must not have been present before the transition.
    assert Phase.SCHEMA.value not in timings_before


def test_phase_timing_is_iso_string() -> None:
    """Timing values are ISO 8601 strings (parseable by datetime.fromisoformat)."""
    from datetime import datetime

    state = _get_state()
    state.advance_phase(Phase.SCHEMA)

    iso_str = state.phase_start_times[Phase.SCHEMA.value]
    # Must not raise — valid ISO 8601
    dt = datetime.fromisoformat(iso_str)
    assert dt is not None


def test_init_timing_set_at_bootstrap() -> None:
    """Phase.INIT timing is recorded at bootstrap, before any advance."""
    state = _get_state()
    assert Phase.INIT.value in state.phase_start_times


# ---------------------------------------------------------------------------
# Test: initialize_session
# ---------------------------------------------------------------------------

def test_initialize_session_sets_run_id() -> None:
    """initialize_session() derives and stores a non-empty run_id."""
    state = _get_state()
    state.initialize_session(
        neo4j_uri="neo4j+s://test.databases.neo4j.io",
        neo4j_user="neo4j",
        neo4j_password="secret",
        openrouter_api_key="sk-or-test",
    )
    assert state.run_id is not None
    assert len(state.run_id) == 64  # SHA-256 hex digest


def test_initialize_session_run_id_is_deterministic() -> None:
    """initialize_session() with the same user always produces the same run_id
    when called with the same timestamp_seed.

    Since timestamp_seed is captured at call time, we verify determinism by
    comparing the ID derivation formula directly against the stored result.
    """
    from api.models.run import derive_run_id

    state = _get_state()
    state.initialize_session(
        neo4j_uri="neo4j+s://test.databases.neo4j.io",
        neo4j_user="alice",
        neo4j_password="pw",
        openrouter_api_key="sk-or-test",
    )
    stored_run_id = state.run_id
    stored_seed = state.timestamp_seed

    # Re-derive from stored seed — must match the stored run_id.
    assert stored_seed is not None
    rederived = derive_run_id("alice", stored_seed)
    assert stored_run_id == rederived


def test_initialize_session_stores_neo4j_uri() -> None:
    """initialize_session() stores the Neo4j URI in session state."""
    state = _get_state()
    state.initialize_session(
        neo4j_uri="neo4j+s://example.databases.neo4j.io",
        neo4j_user="neo4j",
        neo4j_password="pw",
        openrouter_api_key="sk-or-test",
    )
    assert state.neo4j_uri == "neo4j+s://example.databases.neo4j.io"
    assert state.neo4j_user == "neo4j"


# ---------------------------------------------------------------------------
# Test: panel mode tracking
# ---------------------------------------------------------------------------

def test_panel_mode_default_is_write() -> None:
    """Panels default to PANEL_WRITE when no mode has been set."""
    state = _get_state()
    mode = state.get_panel_mode("schema_editor")
    assert mode == PANEL_WRITE


def test_set_and_get_panel_mode_read() -> None:
    """Setting a panel to read mode is reflected by get_panel_mode()."""
    state = _get_state()
    state.set_panel_mode("schema_editor", PANEL_READ)
    assert state.get_panel_mode("schema_editor") == PANEL_READ


def test_set_panel_mode_write_after_read() -> None:
    """Panel mode can transition read → write."""
    state = _get_state()
    state.set_panel_mode("schema_editor", PANEL_READ)
    state.set_panel_mode("schema_editor", PANEL_WRITE)
    assert state.get_panel_mode("schema_editor") == PANEL_WRITE


def test_set_invalid_panel_mode_raises() -> None:
    """An invalid mode string raises ValueError (only 'read'/'write' allowed)."""
    state = _get_state()
    with pytest.raises(ValueError):
        state.set_panel_mode("schema_editor", "edit")


def test_multiple_panels_tracked_independently() -> None:
    """Different panel names maintain independent modes."""
    state = _get_state()
    state.set_panel_mode("panel_a", PANEL_READ)
    state.set_panel_mode("panel_b", PANEL_WRITE)

    assert state.get_panel_mode("panel_a") == PANEL_READ
    assert state.get_panel_mode("panel_b") == PANEL_WRITE


# ---------------------------------------------------------------------------
# Test: _bootstrap idempotency
# ---------------------------------------------------------------------------

def test_bootstrap_does_not_overwrite_existing_state(session_state: dict) -> None:
    """_bootstrap() must not overwrite keys that already exist in session state.

    This ensures that Streamlit reruns do not reset live state.
    """
    # Pre-populate a key as if a previous rerun had already set it.
    from ui.state import _K_PHASE
    session_state[_K_PHASE] = Phase.INGESTION

    # Bootstrap runs again (simulating a Streamlit rerun).
    StateManager._bootstrap()

    # The pre-existing phase must be preserved.
    state = StateManager.get()
    assert state.phase == Phase.INGESTION
