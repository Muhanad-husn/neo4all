"""
tests/integration/test_manual_curation_cycle.py — Full curation pipeline
integration test (SPEC-06 file 21).

Exercises the complete governed mutation pipeline against a real Neo4j Aura
instance:

    candidate → evidence → propose → diff → approve → execute → audit

Requires: NEO4J_CI_URI, NEO4J_CI_USER, NEO4J_CI_PASSWORD environment
variables.  All tests are skipped when NEO4J_CI_URI is absent.

Non-Neo4j dependencies (Redis, S3) are replaced by in-process stubs — this
test is intentionally scoped to validate the Neo4j write paths and invariant
checks inside ExecutionAgent.

Test scenarios:
  1. Canonicalize cycle     — seed node → canonicalize proposal → execute → property updated
  2. Rejected without token — execute without approval in cache → AuditOutcome.rejected
  3. Schema mismatch        — diff schema_version ≠ locked schema → AuditOutcome.rejected
  4. Create node cycle      — create_node step → node present in graph after execution
  5. Delete node cycle      — create node then delete proposal → node absent after execution
  6. Cache invalidation     — successful mutation triggers run-scoped cache invalidation

Clean-up strategy:
  All test nodes are tagged with _run_id = _TEST_RUN_ID.  The module-scoped
  neo4j fixture deletes all nodes with that _run_id after the test session.
"""

from __future__ import annotations

import hashlib
import io
import os
from typing import Any

import pytest
from botocore.exceptions import ClientError

from api.agents.execution import ApprovalRecord, ExecutionAgent
from api.audit.models import AuditOutcome, AuditRecord
from api.cache.keys import CacheKey
from api.diff.builder import DiffBuilder
from api.diff.models import DiffOperation, DiffPlan, DiffStep
from api.graph.client import Neo4jClient
from api.proposals.models import ElementRef, ProposalClass, ProposalPacket, ProposalState
from api.schema.models import NodeTypeDef, SchemaVersion

# ---------------------------------------------------------------------------
# Skip marker — all tests require real Neo4j CI credentials
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.environ.get("NEO4J_CI_URI"),
    reason="NEO4J_CI_URI not set — skipping Neo4j integration tests",
)

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

_TEST_RUN_ID = "ci-spec06-curation-" + hashlib.sha256(b"spec06-manual-curation").hexdigest()[:16]
_TEST_ACTOR = "ci-test-actor"

# Deterministic candidate ID used across all tests in this module.
_CANDIDATE_ID = hashlib.sha256(b"ci-spec06-candidate-fixture").hexdigest()


# ---------------------------------------------------------------------------
# In-process stubs
# ---------------------------------------------------------------------------


class _FakeS3:
    """In-memory S3 stub — supports put_object, get_object, head_object."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "") -> dict:
        self._store[Key] = Body
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict:
        if Key not in self._store:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self._store[Key])}

    def head_object(self, Bucket: str, Key: str) -> dict:
        if Key not in self._store:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        return {"ContentLength": len(self._store[Key])}


class _FakeCache:
    """In-memory cache stub matching the CacheClient interface used by ExecutionAgent."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self.invalidated_prefixes: list[str] = []

    async def get(self, key: str, model: Any = None) -> Any:
        raw = self._store.get(key)
        if raw is None:
            return None
        if model is not None and isinstance(raw, dict):
            return model.model_validate(raw)
        return raw

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        if hasattr(value, "model_dump"):
            self._store[key] = value.model_dump(mode="json")
        else:
            self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def invalidate_prefix(self, prefix: str) -> int:
        before = len(self._store)
        keys_to_remove = [k for k in self._store if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._store[k]
        self.invalidated_prefixes.append(prefix)
        return before - len(self._store)


class _FakeSchemaService:
    """Returns a fixed SchemaVersion; simulates a locked schema for one run."""

    def __init__(self, schema: SchemaVersion) -> None:
        self._schema = schema

    async def get_current(self, run_id: str) -> SchemaVersion | None:
        if run_id == self._schema.run_id:
            return self._schema
        return None


class _FakeProposalService:
    """In-memory proposal store supporting the subset of ProposalService used by Agent-C."""

    def __init__(self) -> None:
        self._proposals: dict[str, ProposalPacket] = {}

    async def create(self, packet: ProposalPacket) -> ProposalPacket:
        self._proposals[packet.proposal_id] = packet
        return packet

    async def get_for_run(self, run_id: str, proposal_id: str) -> ProposalPacket | None:
        return self._proposals.get(proposal_id)

    async def transition(
        self, run_id: str, proposal_id: str, state: ProposalState
    ) -> ProposalPacket:
        existing = self._proposals[proposal_id]
        updated = ProposalPacket.model_validate(
            {**existing.model_dump(mode="json"), "state": state}
        )
        self._proposals[proposal_id] = updated
        return updated


class _FakeAuditWriter:
    """Captures AuditRecord objects in memory instead of writing to S3."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def append(self, record: AuditRecord) -> str:
        self.records.append(record)
        return f"fake/audit/{record.record_id}.json"


# ---------------------------------------------------------------------------
# Shared schema — constructed once for the module
# ---------------------------------------------------------------------------


def _make_schema(run_id: str) -> SchemaVersion:
    return SchemaVersion(
        run_id=run_id,
        node_types=(
            NodeTypeDef(
                node_class="Entity",
                type="Person",
                primary_property="email",
            ),
        ),
        edge_types=(),
    )


# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------


async def _run_write(
    neo4j: Neo4jClient, cypher: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    async def _tx(tx: Any) -> list[dict[str, Any]]:
        result = await tx.run(cypher, **params)
        return await result.data()

    return await neo4j.run_write_transaction(_tx)


async def _run_read(
    neo4j: Neo4jClient, cypher: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    async def _fetch(tx: Any) -> list[dict[str, Any]]:
        result = await tx.run(cypher, **params)
        return await result.data()

    async with neo4j.acquire_session() as session:
        return await session.execute_read(_fetch)


async def _seed_node(
    neo4j: Neo4jClient,
    *,
    dedupe_key: str,
    run_id: str,
    schema_version: str,
    name: str,
) -> None:
    """Write a Person test node directly to Neo4j for test setup."""
    await _run_write(
        neo4j,
        "MERGE (n:Person {_dedupe_key: $dk, _run_id: $run_id}) "
        "ON CREATE SET n.name = $name, n._schema_version = $sv, "
        "              n._created_at = datetime() "
        "ON MATCH  SET n.name = $name, n._last_seen_at = datetime()",
        {"dk": dedupe_key, "run_id": run_id, "sv": schema_version, "name": name},
    )


async def _cleanup(neo4j: Neo4jClient, run_id: str) -> None:
    """Remove all nodes tagged with _run_id from Neo4j."""
    await _run_write(
        neo4j,
        "MATCH (n {_run_id: $run_id}) DETACH DELETE n",
        {"run_id": run_id},
    )


def _derive_approval_id(proposal_id: str, actor: str) -> str:
    """Mirror the approval gate's deterministic token derivation."""
    return hashlib.sha256(f"approval:{proposal_id}:{actor}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def neo4j() -> Neo4jClient:  # type: ignore[misc]
    """Module-scoped real Neo4j client using CI credentials."""
    client = Neo4jClient(
        uri=os.environ["NEO4J_CI_URI"],
        user=os.environ["NEO4J_CI_USER"],
        password=os.environ["NEO4J_CI_PASSWORD"],
    )
    # open() is synchronous and called lazily, but we force it here so that
    # connectivity failures surface at fixture time, not inside a test.
    client.open()
    yield client
    # Best-effort cleanup; ignore errors during teardown.
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_cleanup(client, _TEST_RUN_ID))
        loop.run_until_complete(client.close())
    except Exception:  # noqa: BLE001
        pass
    finally:
        loop.close()


@pytest.fixture()
def schema() -> SchemaVersion:
    return _make_schema(_TEST_RUN_ID)


@pytest.fixture()
def cache() -> _FakeCache:
    return _FakeCache()


@pytest.fixture()
def proposal_svc() -> _FakeProposalService:
    return _FakeProposalService()


@pytest.fixture()
def audit_writer() -> _FakeAuditWriter:
    return _FakeAuditWriter()


@pytest.fixture()
def agent(
    neo4j: Neo4jClient,
    schema: SchemaVersion,
    cache: _FakeCache,
    proposal_svc: _FakeProposalService,
    audit_writer: _FakeAuditWriter,
) -> ExecutionAgent:
    return ExecutionAgent(
        neo4j_client=neo4j,
        schema_service=_FakeSchemaService(schema),
        proposal_service=proposal_svc,
        audit_writer=audit_writer,
        cache=cache,
    )


# ---------------------------------------------------------------------------
# Helper: build an approved proposal + approval token in the stubs
# ---------------------------------------------------------------------------


async def _prepare_approval(
    *,
    proposal: ProposalPacket,
    proposal_svc: _FakeProposalService,
    cache: _FakeCache,
    actor: str = _TEST_ACTOR,
) -> tuple[str, DiffPlan]:
    """Store an approved proposal and inject its ApprovalRecord into cache.

    Returns (approval_id, diff_plan).
    """
    await proposal_svc.create(proposal)
    await proposal_svc.transition(_TEST_RUN_ID, proposal.proposal_id, ProposalState.approved)

    approval_id = _derive_approval_id(proposal.proposal_id, actor)
    approval = ApprovalRecord(
        approval_id=approval_id,
        proposal_id=proposal.proposal_id,
        run_id=proposal.run_id,
        actor=actor,
        is_confirmed=False,
    )
    await cache.set(CacheKey.approval(approval_id), approval)

    diff = DiffBuilder().build(proposal)
    return approval_id, diff


# ---------------------------------------------------------------------------
# Test 1: Full canonicalize cycle — update node property
# ---------------------------------------------------------------------------


async def test_canonicalize_cycle(
    neo4j: Neo4jClient,
    schema: SchemaVersion,
    cache: _FakeCache,
    proposal_svc: _FakeProposalService,
    audit_writer: _FakeAuditWriter,
    agent: ExecutionAgent,
) -> None:
    """Seed a node, canonicalise its name property, verify update and audit.

    Pipeline: seed → propose canonicalize → approve → build diff → execute →
    assert node.name updated, AuditRecord.outcome == applied.
    """
    dedupe_key = f"Person|canon-alice|{schema.version_hash}"
    await _seed_node(
        neo4j,
        dedupe_key=dedupe_key,
        run_id=_TEST_RUN_ID,
        schema_version=schema.version_hash,
        name="alice smith",
    )

    proposal = ProposalPacket(
        run_id=_TEST_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        proposal_class=ProposalClass.canonicalize,
        schema_version=schema.version_hash,
        rationale="Standardise name to title case.",
        targets=(
            ElementRef(
                element_type="node",
                dedupe_key=dedupe_key,
                label="Alice Smith",
                properties={"name": "Alice Smith"},
            ),
        ),
    )

    approval_id, diff = await _prepare_approval(
        proposal=proposal, proposal_svc=proposal_svc, cache=cache
    )

    record = await agent.execute(diff, approval_id, _TEST_ACTOR)

    # Audit record assertions
    assert record.outcome == AuditOutcome.applied
    assert record.steps_applied == 1
    assert record.approval_id == approval_id
    assert record.proposal_id == proposal.proposal_id
    assert record.diff_id == diff.diff_id
    assert record.run_id == _TEST_RUN_ID
    assert len(record.record_id) == 64
    assert len(audit_writer.records) == 1

    # Neo4j graph assertions: property was updated
    rows = await _run_read(
        neo4j,
        "MATCH (n {_dedupe_key: $dk, _run_id: $run_id}) RETURN n.name AS name",
        {"dk": dedupe_key, "run_id": _TEST_RUN_ID},
    )
    assert rows, "Node should exist after canonicalize"
    assert rows[0]["name"] == "Alice Smith"


# ---------------------------------------------------------------------------
# Test 2: Execution rejected without approval token
# ---------------------------------------------------------------------------


async def test_execution_rejected_without_approval(
    neo4j: Neo4jClient,
    schema: SchemaVersion,
    cache: _FakeCache,
    proposal_svc: _FakeProposalService,
    audit_writer: _FakeAuditWriter,
    agent: ExecutionAgent,
) -> None:
    """Executing without an approval_id in cache always produces outcome=rejected.

    The graph must not be mutated — no node is created for this test.
    """
    dedupe_key = f"Person|no-approval-target|{schema.version_hash}"
    proposal = ProposalPacket(
        run_id=_TEST_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        proposal_class=ProposalClass.canonicalize,
        schema_version=schema.version_hash,
        rationale="Should be rejected — no approval present.",
        targets=(
            ElementRef(
                element_type="node",
                dedupe_key=dedupe_key,
                properties={"name": "Rejected Target"},
            ),
        ),
    )
    await proposal_svc.create(proposal)
    # Intentionally NOT injecting approval into cache.
    diff = DiffBuilder().build(proposal)

    fake_approval_id = "nonexistent-approval-id"
    record = await agent.execute(diff, fake_approval_id, _TEST_ACTOR)

    assert record.outcome == AuditOutcome.rejected
    assert record.steps_applied == 0
    assert record.error_detail["code"] == "approval_not_found"
    assert len(audit_writer.records) == 1


# ---------------------------------------------------------------------------
# Test 3: Schema version mismatch is rejected before touching graph
# ---------------------------------------------------------------------------


async def test_schema_version_mismatch_rejected(
    neo4j: Neo4jClient,
    schema: SchemaVersion,
    cache: _FakeCache,
    proposal_svc: _FakeProposalService,
    audit_writer: _FakeAuditWriter,
    agent: ExecutionAgent,
) -> None:
    """A DiffPlan with a stale schema_version is rejected by the pre-execution check.

    The _FakeSchemaService returns the current schema.  We inject a DiffPlan
    whose schema_version field does not match → outcome=rejected.
    """
    dedupe_key = f"Person|mismatch-target|{schema.version_hash}"
    proposal = ProposalPacket(
        run_id=_TEST_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        proposal_class=ProposalClass.canonicalize,
        schema_version=schema.version_hash,
        rationale="Mismatch test proposal.",
        targets=(
            ElementRef(element_type="node", dedupe_key=dedupe_key, properties={"name": "X"}),
        ),
    )
    approval_id, correct_diff = await _prepare_approval(
        proposal=proposal, proposal_svc=proposal_svc, cache=cache
    )

    # Build a DiffPlan with a DIFFERENT schema_version.
    stale_diff = DiffPlan(
        proposal_id=correct_diff.proposal_id,
        run_id=_TEST_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        schema_version="stale-schema-version-xyz",
        steps=correct_diff.steps,
    )

    record = await agent.execute(stale_diff, approval_id, _TEST_ACTOR)

    assert record.outcome == AuditOutcome.rejected
    assert record.error_detail["code"] == "schema_version_mismatch"
    assert record.steps_applied == 0


# ---------------------------------------------------------------------------
# Test 4: create_node cycle — node appears in graph after execution
# ---------------------------------------------------------------------------


async def test_create_node_cycle(
    neo4j: Neo4jClient,
    schema: SchemaVersion,
    cache: _FakeCache,
    proposal_svc: _FakeProposalService,
    audit_writer: _FakeAuditWriter,
    agent: ExecutionAgent,
) -> None:
    """A create_node DiffStep causes a new node to appear in the graph.

    We inject the DiffStep manually (DiffBuilder does not produce create_node
    — that is an Agent-P responsibility for genuinely new nodes).
    """
    dedupe_key = f"Person|created-node-test|{schema.version_hash}"

    # Manually craft a DiffPlan with a create_node step.
    proposal = ProposalPacket(
        run_id=_TEST_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        proposal_class=ProposalClass.canonicalize,
        schema_version=schema.version_hash,
        rationale="Create node test proposal.",
    )
    await proposal_svc.create(proposal)
    await proposal_svc.transition(_TEST_RUN_ID, proposal.proposal_id, ProposalState.approved)

    approval_id = _derive_approval_id(proposal.proposal_id, _TEST_ACTOR)
    await cache.set(
        CacheKey.approval(approval_id),
        ApprovalRecord(
            approval_id=approval_id,
            proposal_id=proposal.proposal_id,
            run_id=_TEST_RUN_ID,
            actor=_TEST_ACTOR,
        ),
    )

    create_step = DiffStep(
        operation=DiffOperation.create_node,
        dedupe_key=dedupe_key,
        node_labels=("Person",),
        properties_set={"name": "Created Node"},
        rationale="create_node integration test",
    )
    diff = DiffPlan(
        proposal_id=proposal.proposal_id,
        run_id=_TEST_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        schema_version=schema.version_hash,
        steps=(create_step,),
    )

    record = await agent.execute(diff, approval_id, _TEST_ACTOR)

    assert record.outcome == AuditOutcome.applied
    assert record.steps_applied == 1

    # Verify the node now exists in Neo4j.
    rows = await _run_read(
        neo4j,
        "MATCH (n {_dedupe_key: $dk, _run_id: $run_id}) RETURN n.name AS name",
        {"dk": dedupe_key, "run_id": _TEST_RUN_ID},
    )
    assert rows, "Newly created node should exist in Neo4j"
    assert rows[0]["name"] == "Created Node"


# ---------------------------------------------------------------------------
# Test 5: delete_node cycle — node absent after execution
# ---------------------------------------------------------------------------


async def test_delete_node_cycle(
    neo4j: Neo4jClient,
    schema: SchemaVersion,
    cache: _FakeCache,
    proposal_svc: _FakeProposalService,
    audit_writer: _FakeAuditWriter,
    agent: ExecutionAgent,
) -> None:
    """A delete proposal removes the targeted node from the graph.

    Pipeline: seed node → delete proposal → execute → node absent.
    """
    dedupe_key = f"Person|delete-target|{schema.version_hash}"
    await _seed_node(
        neo4j,
        dedupe_key=dedupe_key,
        run_id=_TEST_RUN_ID,
        schema_version=schema.version_hash,
        name="to be deleted",
    )

    proposal = ProposalPacket(
        run_id=_TEST_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        proposal_class=ProposalClass.delete,
        schema_version=schema.version_hash,
        rationale="Delete stale test node.",
        targets=(
            ElementRef(element_type="node", dedupe_key=dedupe_key, label="Delete Target"),
        ),
    )
    approval_id, diff = await _prepare_approval(
        proposal=proposal, proposal_svc=proposal_svc, cache=cache
    )

    record = await agent.execute(diff, approval_id, _TEST_ACTOR)

    assert record.outcome == AuditOutcome.applied
    assert record.steps_applied == 1

    # Node must be absent.
    rows = await _run_read(
        neo4j,
        "MATCH (n {_dedupe_key: $dk, _run_id: $run_id}) RETURN count(n) AS c",
        {"dk": dedupe_key, "run_id": _TEST_RUN_ID},
    )
    assert int(rows[0]["c"]) == 0, "Deleted node must not exist"


# ---------------------------------------------------------------------------
# Test 6: cache invalidated after successful execution
# ---------------------------------------------------------------------------


async def test_cache_invalidated_after_execution(
    neo4j: Neo4jClient,
    schema: SchemaVersion,
    cache: _FakeCache,
    proposal_svc: _FakeProposalService,
    audit_writer: _FakeAuditWriter,
    agent: ExecutionAgent,
) -> None:
    """A successful mutation triggers invalidation of run-scoped cache prefixes.

    After Agent-C executes, both gq:{run_id}:* and candidates:{run_id}:* must
    have been invalidated (SKILL-D R-D10).
    """
    dedupe_key = f"Person|cache-inv-target|{schema.version_hash}"
    await _seed_node(
        neo4j,
        dedupe_key=dedupe_key,
        run_id=_TEST_RUN_ID,
        schema_version=schema.version_hash,
        name="cache test node",
    )

    # Pre-populate some run-scoped cache entries.
    gq_key = CacheKey.graph_query(_TEST_RUN_ID, "test-query-hash")
    cand_key = CacheKey.candidates(_TEST_RUN_ID, "test-detect-hash")
    await cache.set(gq_key, {"some": "graph_data"})
    await cache.set(cand_key, {"some": "candidate_data"})

    proposal = ProposalPacket(
        run_id=_TEST_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        proposal_class=ProposalClass.canonicalize,
        schema_version=schema.version_hash,
        rationale="Cache invalidation test.",
        targets=(
            ElementRef(
                element_type="node",
                dedupe_key=dedupe_key,
                properties={"name": "Cache Test Node"},
            ),
        ),
    )
    approval_id, diff = await _prepare_approval(
        proposal=proposal, proposal_svc=proposal_svc, cache=cache
    )

    record = await agent.execute(diff, approval_id, _TEST_ACTOR)

    assert record.outcome == AuditOutcome.applied

    # Both run-scoped prefixes must have been invalidated.
    expected_gq_prefix = CacheKey.graph_query_prefix(_TEST_RUN_ID)
    expected_cand_prefix = CacheKey.candidates_prefix(_TEST_RUN_ID)
    assert expected_gq_prefix in cache.invalidated_prefixes, (
        "graph_query prefix must be invalidated after mutation"
    )
    assert expected_cand_prefix in cache.invalidated_prefixes, (
        "candidates prefix must be invalidated after mutation"
    )
