"""
tests/integration/test_graph_write.py — Neo4j MERGE idempotency (SPEC-04 file 15).

Validates that GraphWriter.write_extraction_result() is structurally idempotent:
  - Writing the same node twice produces exactly one node in the graph.
  - Writing the same edge twice produces exactly one edge in the graph.
  - Result counts on second write report action="merge_matched", not "merge_created".

Prerequisites:
  - NEO4J_CI_URI  — connection URI for the CI Aura instance
  - NEO4J_CI_USER — username (default: "neo4j")
  - NEO4J_CI_PASSWORD — password

All tests are skipped if NEO4J_CI_URI is absent. Each test cleans up its own
nodes via MATCH + DETACH DELETE keyed on _run_id so CI runs do not pollute
the target database.

asyncio_mode = "auto" (pyproject.toml) — no @pytest.mark.asyncio decorators needed.
"""

from __future__ import annotations

import os

import pytest

from api.graph.client import Neo4jClient
from api.graph.writer import GraphWriter
from api.models.extraction import ExtractedEdge, ExtractedNode, ExtractionResult

# ---------------------------------------------------------------------------
# Environment-based skip guard
# ---------------------------------------------------------------------------

_CI_URI = os.environ.get("NEO4J_CI_URI")
_CI_USER = os.environ.get("NEO4J_CI_USER", "neo4j")
_CI_PASSWORD = os.environ.get("NEO4J_CI_PASSWORD", "")

pytestmark = pytest.mark.skipif(
    _CI_URI is None,
    reason="NEO4J_CI_URI not set — skipping Neo4j integration tests",
)

# ---------------------------------------------------------------------------
# Test-run identity
# Unique label suffix prevents collisions with real data in the CI database.
# ---------------------------------------------------------------------------

_TEST_RUN_ID = "integ-spec04-graph-write-00001"
_SCHEMA_VERSION = "0" * 64  # deterministic stand-in for a locked version_hash
_CHUNK_ID_A = "a" * 64
_CHUNK_ID_B = "b" * 64

# Node labels used in this test — valid Neo4j identifiers, CI-test-only names.
_LABEL_PERSON = "IntegTestPerson"
_LABEL_ORG = "IntegTestOrganization"
_REL_TYPE = "IntegTestWorksFor"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def client() -> Neo4jClient:  # type: ignore[return]
    """Open a real Neo4jClient against the CI Aura instance.

    Teardown: delete all nodes tagged with _run_id = _TEST_RUN_ID.
    DETACH DELETE handles any relationships between those nodes.
    """
    if _CI_URI is None:
        pytest.skip("NEO4J_CI_URI not set")

    c = Neo4jClient(uri=_CI_URI, user=_CI_USER, password=_CI_PASSWORD)
    c.open()

    yield c

    # Teardown — runs even if the test fails
    async with c.acquire_session() as session:
        await session.run(
            "MATCH (n {_run_id: $run_id}) DETACH DELETE n",
            run_id=_TEST_RUN_ID,
        )
    await c.close()


@pytest.fixture()
def writer(client: Neo4jClient) -> GraphWriter:
    return GraphWriter(client)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _alice_node(chunk_id: str = _CHUNK_ID_A) -> ExtractedNode:
    return ExtractedNode(
        run_id=_TEST_RUN_ID,
        chunk_id=chunk_id,
        schema_version=_SCHEMA_VERSION,
        node_type=_LABEL_PERSON,
        primary_value="IntegAlice",
    )


def _acme_node(chunk_id: str = _CHUNK_ID_A) -> ExtractedNode:
    return ExtractedNode(
        run_id=_TEST_RUN_ID,
        chunk_id=chunk_id,
        schema_version=_SCHEMA_VERSION,
        node_type=_LABEL_ORG,
        primary_value="IntegAcmeCorp",
    )


def _works_for_edge(chunk_id: str = _CHUNK_ID_A) -> ExtractedEdge:
    return ExtractedEdge(
        run_id=_TEST_RUN_ID,
        chunk_id=chunk_id,
        schema_version=_SCHEMA_VERSION,
        rel_type=_REL_TYPE,
        start_node_type=_LABEL_PERSON,
        start_primary_value="IntegAlice",
        end_node_type=_LABEL_ORG,
        end_primary_value="IntegAcmeCorp",
    )


async def _count_nodes(client: Neo4jClient, node_type: str, primary_value: str) -> int:
    """Return the count of nodes with the given label and _primary_value."""
    async with client.acquire_session() as session:
        result = await session.run(
            f"MATCH (n:`{node_type}` {{_run_id: $run_id, _primary_value: $primary_value}})"
            " RETURN count(n) AS cnt",
            run_id=_TEST_RUN_ID,
            primary_value=primary_value,
        )
        record = await result.single()
        return int(record["cnt"]) if record else 0


async def _count_edges(client: Neo4jClient, rel_type: str) -> int:
    """Return the count of relationships of the given type scoped to the test run."""
    async with client.acquire_session() as session:
        result = await session.run(
            f"MATCH ()-[r:`{rel_type}` {{_run_id: $run_id}}]->() RETURN count(r) AS cnt",
            run_id=_TEST_RUN_ID,
        )
        record = await result.single()
        return int(record["cnt"]) if record else 0


# ---------------------------------------------------------------------------
# Node MERGE idempotency
# ---------------------------------------------------------------------------


class TestNodeMergeIdempotency:
    async def test_write_node_once_creates_one_node(
        self, writer: GraphWriter, client: Neo4jClient
    ) -> None:
        result_1 = await writer.write_node(_alice_node())
        assert result_1.action == "merge_created"
        count = await _count_nodes(client, _LABEL_PERSON, "IntegAlice")
        assert count == 1

    async def test_write_same_node_twice_yields_one_node(
        self, writer: GraphWriter, client: Neo4jClient
    ) -> None:
        """Structural idempotency: MERGE on dedupe_key prevents duplicates."""
        result_1 = await writer.write_node(_alice_node(chunk_id=_CHUNK_ID_A))
        result_2 = await writer.write_node(_alice_node(chunk_id=_CHUNK_ID_B))

        # chunk_id differs — but the dedupe_key (node_type, primary_value, schema_version)
        # is identical, so the second write must match the existing node.
        assert result_1.action == "merge_created"
        assert result_2.action == "merge_matched"

        count = await _count_nodes(client, _LABEL_PERSON, "IntegAlice")
        assert count == 1, (
            f"Expected 1 node after two writes with same dedupe_key; found {count}"
        )

    async def test_write_same_node_three_times_yields_one_node(
        self, writer: GraphWriter, client: Neo4jClient
    ) -> None:
        await writer.write_node(_alice_node())
        await writer.write_node(_alice_node())
        await writer.write_node(_alice_node())
        count = await _count_nodes(client, _LABEL_PERSON, "IntegAlice")
        assert count == 1

    async def test_different_primary_values_create_separate_nodes(
        self, writer: GraphWriter, client: Neo4jClient
    ) -> None:
        bob = ExtractedNode(
            run_id=_TEST_RUN_ID,
            chunk_id=_CHUNK_ID_A,
            schema_version=_SCHEMA_VERSION,
            node_type=_LABEL_PERSON,
            primary_value="IntegBob",
        )
        await writer.write_node(_alice_node())
        await writer.write_node(bob)
        count_alice = await _count_nodes(client, _LABEL_PERSON, "IntegAlice")
        count_bob = await _count_nodes(client, _LABEL_PERSON, "IntegBob")
        assert count_alice == 1
        assert count_bob == 1


# ---------------------------------------------------------------------------
# Edge MERGE idempotency
# ---------------------------------------------------------------------------


class TestEdgeMergeIdempotency:
    async def test_write_same_edge_twice_yields_one_edge(
        self, writer: GraphWriter, client: Neo4jClient
    ) -> None:
        """Structural idempotency: MERGE on (type, start_dedupe_key, end_dedupe_key)."""
        # Nodes must exist first so the MATCH in write_edge succeeds.
        await writer.write_node(_alice_node())
        await writer.write_node(_acme_node())

        result_1 = await writer.write_edge(_works_for_edge(chunk_id=_CHUNK_ID_A))
        result_2 = await writer.write_edge(_works_for_edge(chunk_id=_CHUNK_ID_B))

        assert result_1.action == "merge_created"
        assert result_2.action == "merge_matched"

        count = await _count_edges(client, _REL_TYPE)
        assert count == 1, (
            f"Expected 1 edge after two writes with same dedupe_key; found {count}"
        )

    async def test_edge_skipped_when_start_node_absent(
        self, writer: GraphWriter, client: Neo4jClient
    ) -> None:
        """If the start node doesn't exist, write_edge returns action='skipped'."""
        # Write only the end node — start node absent.
        await writer.write_node(_acme_node())

        result = await writer.write_edge(_works_for_edge())
        assert result.action == "skipped"

        count = await _count_edges(client, _REL_TYPE)
        assert count == 0

    async def test_edge_skipped_when_end_node_absent(
        self, writer: GraphWriter, client: Neo4jClient
    ) -> None:
        """If the end node doesn't exist, write_edge returns action='skipped'."""
        await writer.write_node(_alice_node())
        # End node not written.

        result = await writer.write_edge(_works_for_edge())
        assert result.action == "skipped"

        count = await _count_edges(client, _REL_TYPE)
        assert count == 0


# ---------------------------------------------------------------------------
# Full ExtractionResult round-trip
# ---------------------------------------------------------------------------


class TestExtractionResultRoundTrip:
    async def test_write_extraction_result_idempotent(
        self, writer: GraphWriter, client: Neo4jClient
    ) -> None:
        """write_extraction_result() with same ExtractionResult twice → no duplicates."""
        result = ExtractionResult(
            run_id=_TEST_RUN_ID,
            chunk_id=_CHUNK_ID_A,
            schema_version=_SCHEMA_VERSION,
            nodes=(_alice_node(), _acme_node()),
            edges=(_works_for_edge(),),
        )

        write_result_1 = await writer.write_extraction_result(result)
        write_result_2 = await writer.write_extraction_result(result)

        # First write: everything created
        assert write_result_1.nodes_created == 2
        assert write_result_1.edges_created == 1
        assert write_result_1.nodes_matched == 0

        # Second write: everything matched (no duplicates)
        assert write_result_2.nodes_created == 0
        assert write_result_2.edges_created == 0
        assert write_result_2.nodes_matched == 2
        assert write_result_2.edges_matched == 1

        # Verify actual node count in the graph
        count_alice = await _count_nodes(client, _LABEL_PERSON, "IntegAlice")
        count_acme = await _count_nodes(client, _LABEL_ORG, "IntegAcmeCorp")
        count_edges = await _count_edges(client, _REL_TYPE)

        assert count_alice == 1
        assert count_acme == 1
        assert count_edges == 1

    async def test_empty_result_writes_nothing(
        self, writer: GraphWriter, client: Neo4jClient
    ) -> None:
        result = ExtractionResult(
            run_id=_TEST_RUN_ID,
            chunk_id=_CHUNK_ID_A,
            schema_version=_SCHEMA_VERSION,
        )
        write_result = await writer.write_extraction_result(result)
        assert write_result.nodes_created == 0
        assert write_result.edges_created == 0
