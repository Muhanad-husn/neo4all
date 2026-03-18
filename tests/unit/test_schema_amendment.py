"""
tests/unit/test_schema_amendment.py — Schema amendment model determinism & service tests.

Covers:
  - amendment_id determinism (same input → same hash)
  - amendment_id sensitivity (different edge → different hash)
  - SchemaAmendment is frozen
  - SchemaAmendmentRegistry append behaviour
  - SchemaService.amend_edge guards (not-locked, already-in-base, undefined node type)
  - SchemaService.amend_edge idempotency
  - SchemaService.get_effective_edges returns base + amendments

No network, no LLM, no Neo4j — uses FakeCache for Redis simulation.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from api.schema.amendments import SchemaAmendment, SchemaAmendmentRegistry
from api.schema.models import EdgeTypeDef, NodeTypeDef, SchemaVersion
from api.schema.service import (
    EdgeAlreadyInSchemaError,
    SchemaNotLockedError,
    SchemaService,
    UndefinedNodeTypeError,
)

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-amendment-001"

_NODE_PERSON = NodeTypeDef(
    node_class="Entity", type="Person", primary_property="name"
)
_NODE_ORG = NodeTypeDef(
    node_class="Entity", type="Organization", primary_property="name"
)

_EDGE_WORKS_FOR = EdgeTypeDef(
    start_node_type="Person", end_node_type="Organization", type="WORKS_FOR"
)
_EDGE_WORKS_FOR_REVERSE = EdgeTypeDef(
    start_node_type="Organization", end_node_type="Person", type="WORKS_FOR"
)
_EDGE_MENTORS = EdgeTypeDef(
    start_node_type="Person", end_node_type="Person", type="MENTORS"
)

_SCHEMA = SchemaVersion(
    run_id=_RUN_ID,
    nodes=(_NODE_PERSON, _NODE_ORG),
    edges=(_EDGE_WORKS_FOR, _EDGE_MENTORS),
)


# ---------------------------------------------------------------------------
# Amendment model tests
# ---------------------------------------------------------------------------


class TestSchemaAmendmentModel:
    """Tests for SchemaAmendment and SchemaAmendmentRegistry models."""

    def test_amendment_id_determinism(self) -> None:
        """Same inputs produce the same amendment_id."""
        a1 = SchemaAmendment(
            run_id=_RUN_ID,
            edge_type_def=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
            timestamp_utc="2026-03-18T00:00:00+00:00",
        )
        a2 = SchemaAmendment(
            run_id=_RUN_ID,
            edge_type_def=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
            timestamp_utc="2026-03-18T00:00:00+00:00",
        )
        assert a1.amendment_id == a2.amendment_id
        assert len(a1.amendment_id) == 64  # SHA-256 hex

    def test_amendment_id_matches_reference_formula(self) -> None:
        """amendment_id matches SHA-256(run_id + NUL + type + NUL + start + NUL + end)."""
        a = SchemaAmendment(
            run_id=_RUN_ID,
            edge_type_def=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
            timestamp_utc="2026-03-18T00:00:00+00:00",
        )
        payload = f"{_RUN_ID}\x00WORKS_FOR\x00Organization\x00Person"
        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert a.amendment_id == expected

    def test_amendment_id_sensitivity_to_edge(self) -> None:
        """Different edge types produce different amendment_ids."""
        a1 = SchemaAmendment(
            run_id=_RUN_ID,
            edge_type_def=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
            timestamp_utc="2026-03-18T00:00:00+00:00",
        )
        a2 = SchemaAmendment(
            run_id=_RUN_ID,
            edge_type_def=_EDGE_MENTORS,
            source_candidate_id="cand_002",
            actor="operator",
            timestamp_utc="2026-03-18T00:00:00+00:00",
        )
        assert a1.amendment_id != a2.amendment_id

    def test_amendment_id_sensitivity_to_run_id(self) -> None:
        """Same edge in different runs produces different amendment_ids."""
        a1 = SchemaAmendment(
            run_id="run-a",
            edge_type_def=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
            timestamp_utc="2026-03-18T00:00:00+00:00",
        )
        a2 = SchemaAmendment(
            run_id="run-b",
            edge_type_def=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
            timestamp_utc="2026-03-18T00:00:00+00:00",
        )
        assert a1.amendment_id != a2.amendment_id

    def test_amendment_is_frozen(self) -> None:
        """SchemaAmendment is immutable after construction."""
        a = SchemaAmendment(
            run_id=_RUN_ID,
            edge_type_def=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
            timestamp_utc="2026-03-18T00:00:00+00:00",
        )
        with pytest.raises(ValidationError):
            a.run_id = "different"  # type: ignore[misc]

    def test_registry_append(self) -> None:
        """SchemaAmendmentRegistry can hold multiple amendments."""
        a1 = SchemaAmendment(
            run_id=_RUN_ID,
            edge_type_def=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
            timestamp_utc="2026-03-18T00:00:00+00:00",
        )
        a2 = SchemaAmendment(
            run_id=_RUN_ID,
            edge_type_def=_EDGE_MENTORS,
            source_candidate_id="cand_002",
            actor="operator",
            timestamp_utc="2026-03-18T00:00:00+00:00",
        )
        registry = SchemaAmendmentRegistry(
            run_id=_RUN_ID, amendments=(a1, a2)
        )
        assert len(registry.amendments) == 2
        assert registry.amendments[0].amendment_id == a1.amendment_id
        assert registry.amendments[1].amendment_id == a2.amendment_id

    def test_registry_is_frozen(self) -> None:
        """SchemaAmendmentRegistry is immutable after construction."""
        registry = SchemaAmendmentRegistry(run_id=_RUN_ID)
        with pytest.raises(ValidationError):
            registry.run_id = "different"  # type: ignore[misc]

    def test_amendment_id_excludes_non_identity_fields(self) -> None:
        """Fields not in the identity formula (actor, rationale, timestamp)
        do not affect amendment_id."""
        a1 = SchemaAmendment(
            run_id=_RUN_ID,
            edge_type_def=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="alice",
            timestamp_utc="2026-01-01T00:00:00+00:00",
            rationale="reason A",
        )
        a2 = SchemaAmendment(
            run_id=_RUN_ID,
            edge_type_def=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="bob",
            timestamp_utc="2026-12-31T23:59:59+00:00",
            rationale="reason B",
        )
        assert a1.amendment_id == a2.amendment_id


# ---------------------------------------------------------------------------
# FakeCache for service tests
# ---------------------------------------------------------------------------


class FakeCache:
    """Minimal in-memory cache that simulates get/set/invalidate_prefix."""

    def __init__(self) -> None:
        self._store: dict[str, object] = {}

    async def get(self, key: str, model: type | None = None) -> object | None:
        val = self._store.get(key)
        if val is None:
            return None
        if model is not None and hasattr(model, "model_validate"):
            return model.model_validate(val.model_dump() if hasattr(val, "model_dump") else val)
        return val

    async def set(self, key: str, value: object, ttl: int | None = None) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def invalidate_prefix(self, prefix: str) -> int:
        keys_to_delete = [k for k in self._store if k.startswith(prefix)]
        for k in keys_to_delete:
            del self._store[k]
        return len(keys_to_delete)


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------


class TestSchemaServiceAmendment:
    """Tests for SchemaService amendment methods."""

    @pytest.fixture()
    def fake_cache(self) -> FakeCache:
        return FakeCache()

    @pytest.fixture()
    def service(self, fake_cache: FakeCache) -> SchemaService:
        svc = SchemaService(llm_client=AsyncMock())
        return svc

    @pytest.fixture(autouse=True)
    def _patch_cache(self, fake_cache: FakeCache) -> None:
        with patch("api.schema.service.get_cache_client", return_value=fake_cache):
            yield  # type: ignore[misc]

    async def _lock_schema(self, fake_cache: FakeCache) -> None:
        """Store a locked schema in the fake cache."""
        from api.cache.keys import CacheKey
        await fake_cache.set(CacheKey.schema(_RUN_ID), _SCHEMA, ttl=None)

    @pytest.mark.asyncio()
    async def test_amend_edge_requires_locked_schema(
        self, service: SchemaService
    ) -> None:
        """amend_edge raises SchemaNotLockedError when schema is not locked."""
        with pytest.raises(SchemaNotLockedError):
            await service.amend_edge(
                run_id=_RUN_ID,
                new_edge=_EDGE_WORKS_FOR_REVERSE,
                source_candidate_id="cand_001",
                actor="operator",
            )

    @pytest.mark.asyncio()
    async def test_amend_edge_rejects_undefined_node_type(
        self, service: SchemaService, fake_cache: FakeCache
    ) -> None:
        """amend_edge raises UndefinedNodeTypeError for unknown node types."""
        await self._lock_schema(fake_cache)
        bad_edge = EdgeTypeDef(
            start_node_type="Ghost", end_node_type="Person", type="HAUNTS"
        )
        with pytest.raises(UndefinedNodeTypeError):
            await service.amend_edge(
                run_id=_RUN_ID,
                new_edge=bad_edge,
                source_candidate_id="cand_001",
                actor="operator",
            )

    @pytest.mark.asyncio()
    async def test_amend_edge_rejects_already_in_base(
        self, service: SchemaService, fake_cache: FakeCache
    ) -> None:
        """amend_edge raises EdgeAlreadyInSchemaError for edges in the base schema."""
        await self._lock_schema(fake_cache)
        with pytest.raises(EdgeAlreadyInSchemaError):
            await service.amend_edge(
                run_id=_RUN_ID,
                new_edge=_EDGE_WORKS_FOR,  # already in base schema
                source_candidate_id="cand_001",
                actor="operator",
            )

    @pytest.mark.asyncio()
    async def test_amend_edge_success(
        self, service: SchemaService, fake_cache: FakeCache
    ) -> None:
        """amend_edge stores the amendment and returns it."""
        await self._lock_schema(fake_cache)
        amendment = await service.amend_edge(
            run_id=_RUN_ID,
            new_edge=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
            rationale="Accepting reverse direction",
        )
        assert amendment.amendment_id
        assert amendment.edge_type_def == _EDGE_WORKS_FOR_REVERSE
        assert amendment.actor == "operator"

    @pytest.mark.asyncio()
    async def test_amend_edge_idempotent(
        self, service: SchemaService, fake_cache: FakeCache
    ) -> None:
        """Amending the same edge twice returns the existing amendment."""
        await self._lock_schema(fake_cache)
        a1 = await service.amend_edge(
            run_id=_RUN_ID,
            new_edge=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
        )
        a2 = await service.amend_edge(
            run_id=_RUN_ID,
            new_edge=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_002",
            actor="different_actor",
        )
        assert a1.amendment_id == a2.amendment_id
        # The original amendment is returned, not a new one.
        assert a2.actor == "operator"

    @pytest.mark.asyncio()
    async def test_get_amendments_empty(
        self, service: SchemaService
    ) -> None:
        """get_amendments returns None when no amendments exist."""
        result = await service.get_amendments(_RUN_ID)
        assert result is None

    @pytest.mark.asyncio()
    async def test_get_amendments_after_amend(
        self, service: SchemaService, fake_cache: FakeCache
    ) -> None:
        """get_amendments returns the registry after an amendment."""
        await self._lock_schema(fake_cache)
        await service.amend_edge(
            run_id=_RUN_ID,
            new_edge=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
        )
        registry = await service.get_amendments(_RUN_ID)
        assert registry is not None
        assert len(registry.amendments) == 1

    @pytest.mark.asyncio()
    async def test_get_effective_edges_no_amendments(
        self, service: SchemaService, fake_cache: FakeCache
    ) -> None:
        """get_effective_edges returns base schema edges when no amendments."""
        await self._lock_schema(fake_cache)
        edges = await service.get_effective_edges(_RUN_ID)
        assert len(edges) == 2  # WORKS_FOR + MENTORS

    @pytest.mark.asyncio()
    async def test_get_effective_edges_with_amendment(
        self, service: SchemaService, fake_cache: FakeCache
    ) -> None:
        """get_effective_edges returns base + amendment edges."""
        await self._lock_schema(fake_cache)
        await service.amend_edge(
            run_id=_RUN_ID,
            new_edge=_EDGE_WORKS_FOR_REVERSE,
            source_candidate_id="cand_001",
            actor="operator",
        )
        edges = await service.get_effective_edges(_RUN_ID)
        assert len(edges) == 3  # WORKS_FOR + MENTORS + reverse WORKS_FOR
        edge_keys = {(e.type, e.start_node_type, e.end_node_type) for e in edges}
        assert ("WORKS_FOR", "Organization", "Person") in edge_keys

    @pytest.mark.asyncio()
    async def test_get_effective_edges_no_schema(
        self, service: SchemaService
    ) -> None:
        """get_effective_edges returns empty tuple when no schema is locked."""
        edges = await service.get_effective_edges(_RUN_ID)
        assert edges == ()
