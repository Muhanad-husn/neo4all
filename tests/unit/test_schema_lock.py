"""
tests/unit/test_schema_lock.py — Schema post-lock rejection (SPEC-02 file 8).

Acceptance criteria (SPEC-02, CLAUDE.md §4.4):
  - propose() raises SchemaLockedError when a schema is already cached.
  - approve() raises SchemaLockedError when a schema is already cached.
  - get_current() returns None on cache miss (schema not yet locked).
  - get_current() returns the SchemaVersion on cache hit (schema locked).
  - approve() writes the SchemaVersion to cache on success (no prior lock).
  - SchemaLockedError carries run_id and a valid 64-char version_hash.

No network, no LLM, no Neo4j — the cache client is mocked via unittest.mock.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from api.schema.models import NodeTypeDef, SchemaVersion
from api.schema.service import (
    SchemaLockedError,
    SchemaProposal,
    SchemaService,
)

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-lock-0000000001"

_NODE_PERSON = NodeTypeDef(
    node_class="Entity",
    type="Person",
    primary_property="name",
)


def _make_schema_version(run_id: str = _RUN_ID) -> SchemaVersion:
    """Construct a minimal SchemaVersion for use as a mock cache value."""
    return SchemaVersion(run_id=run_id, nodes=(_NODE_PERSON,), edges=())


def _make_proposal() -> SchemaProposal:
    """Construct a minimal valid SchemaProposal (the approve() input type)."""
    return SchemaProposal.model_validate(
        {
            "nodes": [
                {
                    "node_class": "Entity",
                    "type": "Person",
                    "primary_property": "name",
                }
            ],
            "edges": [],
        }
    )


def _mock_cache(*, locked: bool, run_id: str = _RUN_ID) -> AsyncMock:
    """Return an AsyncMock cache client.

    Args:
        locked: If True, cache.get() returns a SchemaVersion (schema locked).
                If False, cache.get() returns None (schema not yet locked).
        run_id: The run_id used to construct the cached SchemaVersion.
    """
    mock = AsyncMock()
    mock.get.return_value = _make_schema_version(run_id) if locked else None
    mock.set.return_value = None
    return mock


# ---------------------------------------------------------------------------
# Test group 1: get_current — cache miss and hit
# ---------------------------------------------------------------------------


async def test_get_current_returns_none_on_cache_miss() -> None:
    """get_current() returns None when no schema is cached (schema not locked)."""
    service = SchemaService()
    with patch("api.schema.service.get_cache_client", return_value=_mock_cache(locked=False)):
        result = await service.get_current(run_id=_RUN_ID)

    assert result is None


async def test_get_current_returns_schema_on_cache_hit() -> None:
    """get_current() returns the cached SchemaVersion when the schema is locked."""
    expected = _make_schema_version()
    cache = _mock_cache(locked=True)
    cache.get.return_value = expected

    service = SchemaService()
    with patch("api.schema.service.get_cache_client", return_value=cache):
        result = await service.get_current(run_id=_RUN_ID)

    assert result is not None
    assert result.run_id == _RUN_ID
    assert result.version_hash == expected.version_hash


async def test_get_current_hash_is_64_char_hex() -> None:
    """SchemaVersion returned by get_current() has a valid 64-char hex version_hash."""
    service = SchemaService()
    cache = _mock_cache(locked=True)
    with patch("api.schema.service.get_cache_client", return_value=cache):
        result = await service.get_current(run_id=_RUN_ID)

    assert result is not None
    assert len(result.version_hash) == 64
    assert all(c in "0123456789abcdef" for c in result.version_hash)


# ---------------------------------------------------------------------------
# Test group 2: approve — SchemaLockedError when already locked
# ---------------------------------------------------------------------------


async def test_approve_raises_schema_locked_error() -> None:
    """approve() raises SchemaLockedError when a schema is already cached."""
    service = SchemaService()
    with patch("api.schema.service.get_cache_client", return_value=_mock_cache(locked=True)):
        with pytest.raises(SchemaLockedError):
            await service.approve(run_id=_RUN_ID, schema_data=_make_proposal())


async def test_approve_locked_error_carries_run_id() -> None:
    """SchemaLockedError.run_id matches the run_id that was passed to approve()."""
    service = SchemaService()
    with patch("api.schema.service.get_cache_client", return_value=_mock_cache(locked=True)):
        with pytest.raises(SchemaLockedError) as exc_info:
            await service.approve(run_id=_RUN_ID, schema_data=_make_proposal())

    assert exc_info.value.run_id == _RUN_ID


async def test_approve_locked_error_carries_version_hash() -> None:
    """SchemaLockedError.version_hash matches the hash of the cached schema."""
    expected_sv = _make_schema_version()
    cache = _mock_cache(locked=True)
    cache.get.return_value = expected_sv

    service = SchemaService()
    with patch("api.schema.service.get_cache_client", return_value=cache):
        with pytest.raises(SchemaLockedError) as exc_info:
            await service.approve(run_id=_RUN_ID, schema_data=_make_proposal())

    assert exc_info.value.version_hash == expected_sv.version_hash
    assert len(exc_info.value.version_hash) == 64


# ---------------------------------------------------------------------------
# Test group 3: propose — SchemaLockedError when already locked
# ---------------------------------------------------------------------------


async def test_propose_raises_schema_locked_error() -> None:
    """propose() raises SchemaLockedError when a schema is already cached.

    The lock check is the first operation in propose() so the LLM is never
    called — SchemaLockedError is raised before any external communication.
    """
    service = SchemaService()
    with patch("api.schema.service.get_cache_client", return_value=_mock_cache(locked=True)):
        with pytest.raises(SchemaLockedError):
            await service.propose(
                run_id=_RUN_ID,
                domain_description="A biomedical research domain covering trials.",
            )


async def test_propose_locked_error_carries_correct_run_id() -> None:
    """SchemaLockedError raised by propose() carries the correct run_id."""
    service = SchemaService()
    with patch("api.schema.service.get_cache_client", return_value=_mock_cache(locked=True)):
        with pytest.raises(SchemaLockedError) as exc_info:
            await service.propose(
                run_id=_RUN_ID,
                domain_description="A biomedical research domain covering trials.",
            )

    assert exc_info.value.run_id == _RUN_ID


# ---------------------------------------------------------------------------
# Test group 4: approve — success path (no prior lock)
# ---------------------------------------------------------------------------


async def test_approve_returns_schema_version_on_success() -> None:
    """approve() returns a frozen SchemaVersion on first call (no prior lock)."""
    service = SchemaService()
    cache = _mock_cache(locked=False)

    with patch("api.schema.service.get_cache_client", return_value=cache):
        result = await service.approve(run_id=_RUN_ID, schema_data=_make_proposal())

    assert isinstance(result, SchemaVersion)
    assert result.run_id == _RUN_ID


async def test_approve_writes_to_cache_on_success() -> None:
    """approve() calls cache.set() exactly once on a successful lock."""
    service = SchemaService()
    cache = _mock_cache(locked=False)

    with patch("api.schema.service.get_cache_client", return_value=cache):
        await service.approve(run_id=_RUN_ID, schema_data=_make_proposal())

    cache.set.assert_called_once()


async def test_approve_version_hash_deterministic() -> None:
    """Two approve() calls with the same schema_data produce the same version_hash."""
    service = SchemaService()
    proposal = _make_proposal()

    cache_a = _mock_cache(locked=False)
    with patch("api.schema.service.get_cache_client", return_value=cache_a):
        sv_a = await service.approve(run_id=_RUN_ID, schema_data=proposal)

    cache_b = _mock_cache(locked=False)
    with patch("api.schema.service.get_cache_client", return_value=cache_b):
        sv_b = await service.approve(run_id=_RUN_ID, schema_data=proposal)

    assert sv_a.version_hash == sv_b.version_hash


async def test_approve_includes_node_count_in_schema_version() -> None:
    """The SchemaVersion returned by approve() reflects the input node count."""
    service = SchemaService()
    proposal = SchemaProposal.model_validate(
        {
            "nodes": [
                {"node_class": "Entity", "type": "Person", "primary_property": "name"},
                {"node_class": "Entity", "type": "Organization", "primary_property": "name"},
            ],
            "edges": [
                {
                    "start_node_type": "Person",
                    "end_node_type": "Organization",
                    "type": "WORKS_FOR",
                }
            ],
        }
    )

    cache = _mock_cache(locked=False)
    with patch("api.schema.service.get_cache_client", return_value=cache):
        result = await service.approve(run_id=_RUN_ID, schema_data=proposal)

    assert len(result.nodes) == 2
    assert len(result.edges) == 1


# ---------------------------------------------------------------------------
# Test group 5: get_current after approve reflects locked state
# ---------------------------------------------------------------------------


async def test_get_current_reflects_locked_state_after_approve() -> None:
    """A second get_current() with a locked cache returns the approved schema.

    This tests the cache-first contract: once approve() stores the schema,
    subsequent get_current() calls that hit a warmed cache return that schema.
    """
    service = SchemaService()

    # Step 1: approve with unlocked cache.
    cache_unlock = _mock_cache(locked=False)
    with patch("api.schema.service.get_cache_client", return_value=cache_unlock):
        approved_sv = await service.approve(run_id=_RUN_ID, schema_data=_make_proposal())

    # Step 2: get_current with locked cache returning the approved schema.
    cache_lock = _mock_cache(locked=True)
    cache_lock.get.return_value = approved_sv
    with patch("api.schema.service.get_cache_client", return_value=cache_lock):
        retrieved = await service.get_current(run_id=_RUN_ID)

    assert retrieved is not None
    assert retrieved.version_hash == approved_sv.version_hash
