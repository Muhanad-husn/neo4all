"""
tests/unit/test_dedupe_keys.py — Graph writer MERGE dedupe key behavior (SPEC-04 file 13).

Validates that:
  - Same ExtractedNode fields always serialize to the same dedupe key string.
  - Different node fields produce different dedupe keys.
  - The serialized key is consistent with the computed node_dedupe_key tuple.
  - Edge MERGE params carry both endpoint dedupe keys independently.
  - _serialize_rel_dedupe_key encodes both endpoint keys in the result.
  - Swapped edge endpoints produce different rel_dedupe_keys (direction is preserved).
  - CypherInjectionError is raised for identifiers that fail ^[A-Za-z][A-Za-z0-9_]*$.

All assertions are against the Cypher parameter *dict* (not the Cypher string itself)
so tests remain valid even if the query template changes.

No network, no Neo4j, no LLM.
"""

from __future__ import annotations

import pytest

from api.graph.writer import (
    CypherInjectionError,
    _serialize_node_dedupe_key,
    _serialize_rel_dedupe_key,
    _validate_identifier,
)
from api.models.extraction import ExtractedEdge, ExtractedNode

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-dedupe-000001"
_CHUNK_ID = "d" * 64        # 64-char stand-in for SHA-256 hex chunk_id
_SCHEMA_VERSION = "e" * 64  # stand-in for a locked schema version_hash


# ---------------------------------------------------------------------------
# Helpers — replicate the Cypher param dicts built inside GraphWriter
# (testing these inline keeps the tests independent of writer internals)
# ---------------------------------------------------------------------------


def _build_node_params(node: ExtractedNode) -> dict:
    """Mirror the params dict assembled in GraphWriter.write_node()."""
    dedupe_key = _serialize_node_dedupe_key(
        node.node_type, node.primary_value, node.schema_version
    )
    domain_props = {k: v for k, v in node.properties.items() if not k.startswith("_")}
    return {
        "dedupe_key": dedupe_key,
        "domain_props": domain_props,
        "primary_value": node.primary_value,
        "run_id": node.run_id,
        "schema_version": node.schema_version,
        "chunk_id": node.chunk_id,
    }


def _build_edge_params(edge: ExtractedEdge) -> dict:
    """Mirror the params dict assembled in GraphWriter.write_edge()."""
    start_dedupe_key = _serialize_node_dedupe_key(
        edge.start_node_type, edge.start_primary_value, edge.schema_version
    )
    end_dedupe_key = _serialize_node_dedupe_key(
        edge.end_node_type, edge.end_primary_value, edge.schema_version
    )
    rel_dedupe_key_str = _serialize_rel_dedupe_key(edge.rel_dedupe_key)
    domain_props = {k: v for k, v in edge.properties.items() if not k.startswith("_")}
    return {
        "start_dedupe_key": start_dedupe_key,
        "end_dedupe_key": end_dedupe_key,
        "rel_dedupe_key": rel_dedupe_key_str,
        "domain_props": domain_props,
        "run_id": edge.run_id,
        "schema_version": edge.schema_version,
        "chunk_id": edge.chunk_id,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def alice() -> ExtractedNode:
    return ExtractedNode(
        run_id=_RUN_ID,
        chunk_id=_CHUNK_ID,
        schema_version=_SCHEMA_VERSION,
        node_type="Person",
        primary_value="Alice",
    )


@pytest.fixture()
def alice_duplicate() -> ExtractedNode:
    """Identical to alice — same dedupe key expected."""
    return ExtractedNode(
        run_id=_RUN_ID,
        chunk_id=_CHUNK_ID,
        schema_version=_SCHEMA_VERSION,
        node_type="Person",
        primary_value="Alice",
    )


@pytest.fixture()
def bob() -> ExtractedNode:
    return ExtractedNode(
        run_id=_RUN_ID,
        chunk_id=_CHUNK_ID,
        schema_version=_SCHEMA_VERSION,
        node_type="Person",
        primary_value="Bob",
    )


@pytest.fixture()
def acme() -> ExtractedNode:
    return ExtractedNode(
        run_id=_RUN_ID,
        chunk_id=_CHUNK_ID,
        schema_version=_SCHEMA_VERSION,
        node_type="Organization",
        primary_value="Acme Corp",
    )


@pytest.fixture()
def works_for() -> ExtractedEdge:
    return ExtractedEdge(
        run_id=_RUN_ID,
        chunk_id=_CHUNK_ID,
        schema_version=_SCHEMA_VERSION,
        rel_type="WORKS_FOR",
        start_node_type="Person",
        start_primary_value="Alice",
        end_node_type="Organization",
        end_primary_value="Acme Corp",
    )


@pytest.fixture()
def works_for_duplicate() -> ExtractedEdge:
    """Identical to works_for — same rel_dedupe_key expected."""
    return ExtractedEdge(
        run_id=_RUN_ID,
        chunk_id=_CHUNK_ID,
        schema_version=_SCHEMA_VERSION,
        rel_type="WORKS_FOR",
        start_node_type="Person",
        start_primary_value="Alice",
        end_node_type="Organization",
        end_primary_value="Acme Corp",
    )


# ---------------------------------------------------------------------------
# Node dedupe key serialization
# ---------------------------------------------------------------------------


class TestNodeDedupeKeySerialization:
    def test_same_inputs_produce_same_key(self, alice: ExtractedNode) -> None:
        key1 = _serialize_node_dedupe_key(
            alice.node_type, alice.primary_value, alice.schema_version
        )
        key2 = _serialize_node_dedupe_key(
            alice.node_type, alice.primary_value, alice.schema_version
        )
        assert key1 == key2

    def test_different_primary_values_produce_different_keys(
        self, alice: ExtractedNode, bob: ExtractedNode
    ) -> None:
        key_alice = _serialize_node_dedupe_key(
            alice.node_type, alice.primary_value, alice.schema_version
        )
        key_bob = _serialize_node_dedupe_key(
            bob.node_type, bob.primary_value, bob.schema_version
        )
        assert key_alice != key_bob

    def test_different_node_types_produce_different_keys(
        self, alice: ExtractedNode, acme: ExtractedNode
    ) -> None:
        # Same primary_value, different node_type — key must differ.
        key_person = _serialize_node_dedupe_key(
            alice.node_type, "SameValue", alice.schema_version
        )
        key_org = _serialize_node_dedupe_key(
            acme.node_type, "SameValue", acme.schema_version
        )
        assert key_person != key_org

    def test_different_schema_versions_produce_different_keys(self) -> None:
        sv_a = "a" * 64
        sv_b = "b" * 64
        key_a = _serialize_node_dedupe_key("Person", "Alice", sv_a)
        key_b = _serialize_node_dedupe_key("Person", "Alice", sv_b)
        assert key_a != key_b

    def test_key_matches_computed_field_pipe_join(
        self, alice: ExtractedNode
    ) -> None:
        """_serialize_node_dedupe_key must produce the same string as
        '|'.join(node.node_dedupe_key) — edge writes depend on this identity."""
        expected = "|".join(alice.node_dedupe_key)
        actual = _serialize_node_dedupe_key(
            alice.node_type, alice.primary_value, alice.schema_version
        )
        assert actual == expected

    def test_key_components_separated_by_pipe(self, alice: ExtractedNode) -> None:
        key = _serialize_node_dedupe_key(
            alice.node_type, alice.primary_value, alice.schema_version
        )
        parts = key.split("|")
        assert parts[0] == "Person"
        assert parts[1] == "Alice"
        assert parts[2] == _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Node Cypher parameter dict equality
# ---------------------------------------------------------------------------


class TestNodeCypherParams:
    def test_identical_nodes_produce_identical_params(
        self, alice: ExtractedNode, alice_duplicate: ExtractedNode
    ) -> None:
        params_a = _build_node_params(alice)
        params_b = _build_node_params(alice_duplicate)
        assert params_a == params_b

    def test_different_primary_value_produces_different_dedupe_key_in_params(
        self, alice: ExtractedNode, bob: ExtractedNode
    ) -> None:
        params_alice = _build_node_params(alice)
        params_bob = _build_node_params(bob)
        assert params_alice["dedupe_key"] != params_bob["dedupe_key"]

    def test_params_contain_all_required_keys(self, alice: ExtractedNode) -> None:
        params = _build_node_params(alice)
        for required_key in (
            "dedupe_key", "domain_props", "primary_value",
            "run_id", "schema_version", "chunk_id",
        ):
            assert required_key in params, f"Missing param key: {required_key}"

    def test_underscore_properties_stripped_from_domain_props(self) -> None:
        """Properties prefixed with '_' are writer-managed — never passed as domain data."""
        node = ExtractedNode(
            run_id=_RUN_ID,
            chunk_id=_CHUNK_ID,
            schema_version=_SCHEMA_VERSION,
            node_type="Person",
            primary_value="Alice",
            properties={"role": "engineer", "_internal": "managed_by_writer"},
        )
        params = _build_node_params(node)
        assert "role" in params["domain_props"]
        assert "_internal" not in params["domain_props"]

    def test_clean_properties_pass_through_unchanged(self) -> None:
        node = ExtractedNode(
            run_id=_RUN_ID,
            chunk_id=_CHUNK_ID,
            schema_version=_SCHEMA_VERSION,
            node_type="Person",
            primary_value="Alice",
            properties={"role": "engineer", "department": "R&D"},
        )
        params = _build_node_params(node)
        assert params["domain_props"] == {"role": "engineer", "department": "R&D"}


# ---------------------------------------------------------------------------
# Edge dedupe key — both endpoint node dedupe keys encoded
# ---------------------------------------------------------------------------


class TestEdgeDedupeKey:
    def test_rel_dedupe_key_is_4_tuple(self, works_for: ExtractedEdge) -> None:
        assert len(works_for.rel_dedupe_key) == 4

    def test_same_edges_produce_same_rel_dedupe_key(
        self, works_for: ExtractedEdge, works_for_duplicate: ExtractedEdge
    ) -> None:
        assert works_for.rel_dedupe_key == works_for_duplicate.rel_dedupe_key

    def test_start_key_matches_node_serialization(
        self, works_for: ExtractedEdge
    ) -> None:
        expected_start = _serialize_node_dedupe_key(
            works_for.start_node_type,
            works_for.start_primary_value,
            works_for.schema_version,
        )
        _, start_key, _, _ = works_for.rel_dedupe_key
        assert start_key == expected_start

    def test_end_key_matches_node_serialization(
        self, works_for: ExtractedEdge
    ) -> None:
        expected_end = _serialize_node_dedupe_key(
            works_for.end_node_type,
            works_for.end_primary_value,
            works_for.schema_version,
        )
        _, _, end_key, _ = works_for.rel_dedupe_key
        assert end_key == expected_end

    def test_swapped_endpoints_produce_different_key(self) -> None:
        """Edge direction is encoded — (A→B) and (B→A) must not dedupe to the same key."""
        edge_fwd = ExtractedEdge(
            run_id=_RUN_ID, chunk_id=_CHUNK_ID, schema_version=_SCHEMA_VERSION,
            rel_type="KNOWS",
            start_node_type="Person", start_primary_value="Alice",
            end_node_type="Person", end_primary_value="Bob",
        )
        edge_rev = ExtractedEdge(
            run_id=_RUN_ID, chunk_id=_CHUNK_ID, schema_version=_SCHEMA_VERSION,
            rel_type="KNOWS",
            start_node_type="Person", start_primary_value="Bob",
            end_node_type="Person", end_primary_value="Alice",
        )
        assert edge_fwd.rel_dedupe_key != edge_rev.rel_dedupe_key

    def test_different_rel_types_produce_different_keys(self) -> None:
        edge_a = ExtractedEdge(
            run_id=_RUN_ID, chunk_id=_CHUNK_ID, schema_version=_SCHEMA_VERSION,
            rel_type="WORKS_FOR",
            start_node_type="Person", start_primary_value="Alice",
            end_node_type="Organization", end_primary_value="Acme Corp",
        )
        edge_b = ExtractedEdge(
            run_id=_RUN_ID, chunk_id=_CHUNK_ID, schema_version=_SCHEMA_VERSION,
            rel_type="FOUNDED",
            start_node_type="Person", start_primary_value="Alice",
            end_node_type="Organization", end_primary_value="Acme Corp",
        )
        assert edge_a.rel_dedupe_key != edge_b.rel_dedupe_key


# ---------------------------------------------------------------------------
# Edge Cypher parameter dict — carries both endpoint dedupe keys
# ---------------------------------------------------------------------------


class TestEdgeCypherParams:
    def test_params_contain_both_endpoint_keys(self, works_for: ExtractedEdge) -> None:
        params = _build_edge_params(works_for)
        assert "start_dedupe_key" in params
        assert "end_dedupe_key" in params

    def test_start_and_end_keys_are_distinct(self, works_for: ExtractedEdge) -> None:
        """Person/Alice and Organization/AcmeCorp must produce different keys."""
        params = _build_edge_params(works_for)
        assert params["start_dedupe_key"] != params["end_dedupe_key"]

    def test_start_key_encodes_start_node(self, works_for: ExtractedEdge) -> None:
        params = _build_edge_params(works_for)
        assert "Person" in params["start_dedupe_key"]
        assert "Alice" in params["start_dedupe_key"]

    def test_end_key_encodes_end_node(self, works_for: ExtractedEdge) -> None:
        params = _build_edge_params(works_for)
        assert "Organization" in params["end_dedupe_key"]
        assert "Acme Corp" in params["end_dedupe_key"]

    def test_identical_edges_produce_identical_params(
        self, works_for: ExtractedEdge, works_for_duplicate: ExtractedEdge
    ) -> None:
        assert _build_edge_params(works_for) == _build_edge_params(works_for_duplicate)

    def test_params_contain_all_required_keys(self, works_for: ExtractedEdge) -> None:
        params = _build_edge_params(works_for)
        for required_key in (
            "start_dedupe_key", "end_dedupe_key", "rel_dedupe_key",
            "domain_props", "run_id", "schema_version", "chunk_id",
        ):
            assert required_key in params, f"Missing param key: {required_key}"


# ---------------------------------------------------------------------------
# Identifier validation — Cypher injection guard
# ---------------------------------------------------------------------------


class TestValidateIdentifier:
    @pytest.mark.parametrize(
        "valid_id",
        ["Person", "WORKS_FOR", "Neo4jLabel", "Type_With_Underscores", "A", "a1b2c3"],
    )
    def test_valid_identifiers_pass_unchanged(self, valid_id: str) -> None:
        result = _validate_identifier(valid_id, "node_type")
        assert result == valid_id

    @pytest.mark.parametrize(
        "invalid_id",
        [
            "1StartsWithDigit",
            "has space",
            "has-hyphen",
            "has.dot",
            "",
            "_underscore_start",
            "backtick`injection",
            "semi;colon",
            "dollar$sign",
        ],
    )
    def test_invalid_identifiers_raise_cypher_injection_error(
        self, invalid_id: str
    ) -> None:
        with pytest.raises(CypherInjectionError):
            _validate_identifier(invalid_id, "node_type")

    def test_cypher_injection_error_is_value_error_subclass(self) -> None:
        with pytest.raises(ValueError):
            _validate_identifier("invalid identifier", "node_type")

    def test_error_message_includes_kind_and_value(self) -> None:
        with pytest.raises(CypherInjectionError) as exc_info:
            _validate_identifier("bad-value", "rel_type")
        msg = str(exc_info.value)
        assert "rel_type" in msg
        assert "bad-value" in msg
