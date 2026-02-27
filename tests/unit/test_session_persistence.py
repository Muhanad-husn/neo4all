"""
tests/unit/test_session_persistence.py — Session memory persistence tests.

Covers:
  - SessionRecord model validation (no credentials stored)
  - CacheKey.session() determinism and format
  - StateManager.restore_session() bypasses advance_phase() ordering
  - StateManager.restore_session() rejects invalid phase values
  - StateManager.reset() returns all keys to defaults
  - _derive_user_hash() determinism and collision resistance
  - Dirty-check snapshot hash stability

No network, no LLM, no Neo4j — pure in-process tests.
"""

import hashlib

import pytest

from api.cache.keys import CacheKey
from api.models.run import Phase
from api.routers.session_models import SessionRecord


# ---------------------------------------------------------------------------
# Fixture: replace st.session_state with a plain dict (same as
# test_state_manager.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def session_state(monkeypatch):
    """Provide a fresh dict as st.session_state for each test."""
    import ui.state as state_module
    from unittest.mock import MagicMock

    state: dict = {}
    mock_st = MagicMock()
    mock_st.session_state = state
    monkeypatch.setattr(state_module, "st", mock_st)
    return state


def _get_state():
    """Bootstrap and return a fresh StateManager."""
    from ui.state import StateManager
    return StateManager.get()


# ---------------------------------------------------------------------------
# SessionRecord model tests
# ---------------------------------------------------------------------------

class TestSessionRecord:
    """SessionRecord must contain no credentials and roundtrip through JSON."""

    def test_no_password_field(self) -> None:
        """SessionRecord has no password or api_key fields."""
        field_names = set(SessionRecord.model_fields.keys())
        assert "neo4j_password" not in field_names
        assert "openrouter_api_key" not in field_names
        assert "password" not in field_names
        assert "api_key" not in field_names

    def test_roundtrip_json(self) -> None:
        """Serialize to JSON and back — all fields preserved."""
        record = SessionRecord(
            run_id="abc123",
            timestamp_seed="2026-02-27T00:00:00+00:00",
            phase=2,
            schema_version="v1.0",
            neo4j_uri="neo4j+s://test.databases.neo4j.io",
            neo4j_user="neo4j",
        )
        json_bytes = record.model_dump_json()
        restored = SessionRecord.model_validate_json(json_bytes)
        assert restored == record

    def test_phase_stored_as_integer(self) -> None:
        """Phase is stored as an int, not an enum name."""
        record = SessionRecord(
            run_id="r",
            timestamp_seed="ts",
            phase=3,
            neo4j_uri="uri",
            neo4j_user="user",
        )
        dumped = record.model_dump()
        assert isinstance(dumped["phase"], int)
        assert dumped["phase"] == 3

    def test_schema_version_optional(self) -> None:
        """schema_version defaults to None when omitted."""
        record = SessionRecord(
            run_id="r",
            timestamp_seed="ts",
            phase=1,
            neo4j_uri="uri",
            neo4j_user="user",
        )
        assert record.schema_version is None


# ---------------------------------------------------------------------------
# CacheKey.session() tests
# ---------------------------------------------------------------------------

class TestCacheKeySession:
    """CacheKey.session() must be deterministic and properly formatted."""

    def test_deterministic(self) -> None:
        """Same user_hash always produces same key."""
        h = "abcdef1234567890"
        assert CacheKey.session(h) == CacheKey.session(h)

    def test_format(self) -> None:
        """Key starts with 'session:' prefix."""
        key = CacheKey.session("test_hash")
        assert key == "session:test_hash"
        assert key.startswith("session:")

    def test_different_hashes_different_keys(self) -> None:
        """Different user_hashes produce different keys."""
        assert CacheKey.session("hash_a") != CacheKey.session("hash_b")


# ---------------------------------------------------------------------------
# StateManager.restore_session() tests
# ---------------------------------------------------------------------------

class TestRestoreSession:
    """restore_session() reconstitutes a known-valid snapshot."""

    def test_sets_phase_directly(self) -> None:
        """Can jump to Phase 3 without stepping through 1 and 2."""
        state = _get_state()
        assert state.phase == Phase.INIT

        state.restore_session(
            run_id="run123",
            timestamp_seed="2026-01-01T00:00:00+00:00",
            phase=3,
            schema_version="v1",
            neo4j_uri="neo4j+s://test.neo4j.io",
            neo4j_user="neo4j",
            neo4j_password="secret",
            openrouter_api_key="sk-or-test",
        )
        assert state.phase == Phase.EXTRACTION

    def test_sets_all_fields(self) -> None:
        """All fields are correctly populated after restore."""
        state = _get_state()
        state.restore_session(
            run_id="run_abc",
            timestamp_seed="2026-02-27T12:00:00+00:00",
            phase=2,
            schema_version="v2.0",
            neo4j_uri="neo4j+s://prod.neo4j.io",
            neo4j_user="alice",
            neo4j_password="pwd",
            openrouter_api_key="sk-or-key",
        )
        assert state.run_id == "run_abc"
        assert state.timestamp_seed == "2026-02-27T12:00:00+00:00"
        assert state.phase == Phase.INGESTION
        assert state.schema_version == "v2.0"
        assert state.neo4j_uri == "neo4j+s://prod.neo4j.io"
        assert state.neo4j_user == "alice"
        assert state.neo4j_password == "pwd"
        assert state.openrouter_api_key == "sk-or-key"

    def test_invalid_phase_raises(self) -> None:
        """Phase value 99 is not a valid Phase member."""
        state = _get_state()
        with pytest.raises(ValueError):
            state.restore_session(
                run_id="r",
                timestamp_seed="ts",
                phase=99,
                schema_version=None,
                neo4j_uri="uri",
                neo4j_user="user",
                neo4j_password="pwd",
                openrouter_api_key="key",
            )

    def test_advance_after_restore(self) -> None:
        """After restoring to Phase 2, advance_phase(EXTRACTION) works."""
        state = _get_state()
        state.restore_session(
            run_id="r",
            timestamp_seed="ts",
            phase=2,
            schema_version="v1",
            neo4j_uri="uri",
            neo4j_user="user",
            neo4j_password="pwd",
            openrouter_api_key="key",
        )
        assert state.phase == Phase.INGESTION
        state.advance_phase(Phase.EXTRACTION)
        assert state.phase == Phase.EXTRACTION

    def test_schema_version_none(self) -> None:
        """schema_version can be None (Phase 0 restore before schema lock)."""
        state = _get_state()
        state.restore_session(
            run_id="r",
            timestamp_seed="ts",
            phase=1,
            schema_version=None,
            neo4j_uri="uri",
            neo4j_user="user",
            neo4j_password="pwd",
            openrouter_api_key="key",
        )
        assert state.schema_version is None


# ---------------------------------------------------------------------------
# StateManager.reset() tests
# ---------------------------------------------------------------------------

class TestReset:
    """reset() clears all session state back to bootstrap defaults."""

    def test_returns_to_init(self) -> None:
        """After reset(), phase is INIT and run_id is None."""
        state = _get_state()
        state.restore_session(
            run_id="r",
            timestamp_seed="ts",
            phase=3,
            schema_version="v1",
            neo4j_uri="uri",
            neo4j_user="user",
            neo4j_password="pwd",
            openrouter_api_key="key",
        )
        assert state.phase == Phase.EXTRACTION

        state.reset()
        assert state.phase == Phase.INIT
        assert state.run_id is None

    def test_clears_all_fields(self) -> None:
        """All _sm_* keys return to defaults."""
        state = _get_state()
        state.restore_session(
            run_id="r",
            timestamp_seed="ts",
            phase=2,
            schema_version="v2",
            neo4j_uri="uri",
            neo4j_user="user",
            neo4j_password="pwd",
            openrouter_api_key="key",
        )
        state.reset()

        assert state.timestamp_seed is None
        assert state.schema_version is None
        assert state.neo4j_uri is None
        assert state.neo4j_user is None
        assert state.neo4j_password is None
        assert state.openrouter_api_key is None
        assert state.last_saved_hash is None

    def test_clears_schema_proposal(self) -> None:
        """proposed_nodes/edges and proposal_version are reset."""
        state = _get_state()
        state.set_proposed_schema(
            [{"name": "Person"}],
            [{"name": "KNOWS"}],
        )
        assert state.proposed_nodes is not None
        assert state.proposal_version == 1

        state.reset()
        assert state.proposed_nodes is None
        assert state.proposed_edges is None
        assert state.proposal_version == 0


# ---------------------------------------------------------------------------
# _derive_user_hash() tests (via ui/app.py)
# ---------------------------------------------------------------------------

class TestDeriveUserHash:
    """User hash must be deterministic and collision-resistant."""

    def test_deterministic(self) -> None:
        """Same inputs produce same hash."""
        from ui.app import _derive_user_hash
        h1 = _derive_user_hash("neo4j+s://a.neo4j.io", "neo4j")
        h2 = _derive_user_hash("neo4j+s://a.neo4j.io", "neo4j")
        assert h1 == h2

    def test_different_uri_different_hash(self) -> None:
        """Same user, different URI produces different hash."""
        from ui.app import _derive_user_hash
        h1 = _derive_user_hash("neo4j+s://a.neo4j.io", "neo4j")
        h2 = _derive_user_hash("neo4j+s://b.neo4j.io", "neo4j")
        assert h1 != h2

    def test_different_user_different_hash(self) -> None:
        """Same URI, different user produces different hash."""
        from ui.app import _derive_user_hash
        h1 = _derive_user_hash("neo4j+s://a.neo4j.io", "alice")
        h2 = _derive_user_hash("neo4j+s://a.neo4j.io", "bob")
        assert h1 != h2

    def test_matches_reference_formula(self) -> None:
        """Hash matches SHA-256(uri + NUL + user)."""
        from ui.app import _derive_user_hash
        uri = "neo4j+s://test.neo4j.io"
        user = "neo4j"
        expected = hashlib.sha256(f"{uri}\x00{user}".encode()).hexdigest()
        assert _derive_user_hash(uri, user) == expected


# ---------------------------------------------------------------------------
# Dirty-check snapshot hash tests
# ---------------------------------------------------------------------------

class TestDirtyCheck:
    """last_saved_hash tracks session save state."""

    def test_default_is_none(self) -> None:
        """Fresh StateManager has no saved hash."""
        state = _get_state()
        assert state.last_saved_hash is None

    def test_set_and_read(self) -> None:
        """set_last_saved_hash() persists and is readable."""
        state = _get_state()
        state.set_last_saved_hash("abc123")
        assert state.last_saved_hash == "abc123"

    def test_reset_clears_hash(self) -> None:
        """reset() clears the last_saved_hash."""
        state = _get_state()
        state.set_last_saved_hash("xyz")
        state.reset()
        assert state.last_saved_hash is None
