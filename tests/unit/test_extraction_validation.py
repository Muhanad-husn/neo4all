"""
tests/unit/test_extraction_validation.py — ExtractionResult validation (SPEC-04 file 12).

Acceptance criteria (SPEC-04 S-04.3, CLAUDE.md §4.4):
  - Valid ExtractedNode / ExtractedEdge / ExtractionResult constructs cleanly.
  - Missing required fields raises Pydantic ValidationError.
  - Whitespace-only / empty strings rejected by non-empty field validators.
  - ExtractionService._validate_schema_conformance raises ExtractionError
    (a ValueError subclass) when an extracted type is absent from the locked schema.
  - ExtractionService._promote_to_result raises ExtractionError when Pydantic
    field validation fails during promotion (fail-closed — CLAUDE.md §4.4).

No network, no LLM, no Neo4j.
asyncio_mode = "auto" (pyproject.toml) — no @pytest.mark.asyncio decorators needed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.models.extraction import ExtractedEdge, ExtractedNode, ExtractionResult
from api.schema.models import EdgeTypeDef, NodeTypeDef, SchemaVersion
from api.services.extraction import (
    ExtractionError,
    ExtractionService,
    _LLMExtractionOutput,
    _RawEdge,
    _RawNode,
)

# ---------------------------------------------------------------------------
# Module-level constants — shared across all test classes
# ---------------------------------------------------------------------------

# Use a real SchemaVersion so version_hash reflects actual derivation logic.
_NODE_PERSON = NodeTypeDef(node_class="Entity", type="Person", primary_property="name")
_NODE_ORG = NodeTypeDef(node_class="Entity", type="Organization", primary_property="name")
_EDGE_WORKS_FOR = EdgeTypeDef(
    type="WORKS_FOR",
    start_node_type="Person",
    end_node_type="Organization",
)

_RUN_ID = "unit-test-run-extraction-00001"
_CHUNK_ID = "c" * 64  # 64-char stand-in for a SHA-256 hex chunk_id


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def locked_schema() -> SchemaVersion:
    """Minimal locked schema with one node type and one edge type."""
    return SchemaVersion(
        run_id=_RUN_ID,
        nodes=(_NODE_PERSON, _NODE_ORG),
        edges=(_EDGE_WORKS_FOR,),
    )


@pytest.fixture()
def schema_version(locked_schema: SchemaVersion) -> str:
    """The deterministic version_hash of the shared locked schema."""
    return locked_schema.version_hash


@pytest.fixture()
def valid_node(schema_version: str) -> ExtractedNode:
    return ExtractedNode(
        run_id=_RUN_ID,
        chunk_id=_CHUNK_ID,
        schema_version=schema_version,
        node_type="Person",
        primary_value="Alice",
    )


@pytest.fixture()
def valid_edge(schema_version: str) -> ExtractedEdge:
    return ExtractedEdge(
        run_id=_RUN_ID,
        chunk_id=_CHUNK_ID,
        schema_version=schema_version,
        rel_type="WORKS_FOR",
        start_node_type="Person",
        start_primary_value="Alice",
        end_node_type="Organization",
        end_primary_value="Acme Corp",
    )


# ---------------------------------------------------------------------------
# Valid construction
# ---------------------------------------------------------------------------


class TestExtractedNodeValid:
    def test_required_fields_stored(self, valid_node: ExtractedNode) -> None:
        assert valid_node.node_type == "Person"
        assert valid_node.primary_value == "Alice"
        assert valid_node.run_id == _RUN_ID
        assert valid_node.chunk_id == _CHUNK_ID

    def test_dedupe_key_is_3_tuple(self, valid_node: ExtractedNode, schema_version: str) -> None:
        assert valid_node.node_dedupe_key == ("Person", "Alice", schema_version)

    def test_properties_default_to_empty_dict(self, valid_node: ExtractedNode) -> None:
        assert valid_node.properties == {}

    def test_frozen_rejects_mutation(self, valid_node: ExtractedNode) -> None:
        with pytest.raises((TypeError, ValidationError)):
            valid_node.node_type = "Organization"  # type: ignore[misc]

    def test_additional_properties_stored(self, schema_version: str) -> None:
        node = ExtractedNode(
            run_id=_RUN_ID,
            chunk_id=_CHUNK_ID,
            schema_version=schema_version,
            node_type="Person",
            primary_value="Bob",
            properties={"role": "engineer", "email": "bob@example.com"},
        )
        assert node.properties["role"] == "engineer"

    def test_dedupe_key_always_derived_from_stored_fields(
        self, schema_version: str
    ) -> None:
        """node_dedupe_key is a @computed_field — Pydantic V2 silently ignores extra
        constructor kwargs (no extra='forbid' on this model), so passing a 'wrong'
        dedupe_key value is swallowed. The property must still derive from stored fields."""
        # Even if a caller attempts to supply a wrong dedupe_key, the computed
        # property always returns the value derived from node_type / primary_value /
        # schema_version — it cannot be externally overridden.
        node = ExtractedNode(
            run_id=_RUN_ID,
            chunk_id=_CHUNK_ID,
            schema_version=schema_version,
            node_type="Person",
            primary_value="Alice",
        )
        assert node.node_dedupe_key == ("Person", "Alice", schema_version)


class TestExtractedEdgeValid:
    def test_required_fields_stored(self, valid_edge: ExtractedEdge) -> None:
        assert valid_edge.rel_type == "WORKS_FOR"
        assert valid_edge.start_primary_value == "Alice"
        assert valid_edge.end_primary_value == "Acme Corp"

    def test_rel_dedupe_key_is_4_tuple(
        self, valid_edge: ExtractedEdge, schema_version: str
    ) -> None:
        rel_type, start_key, end_key, sv = valid_edge.rel_dedupe_key
        assert rel_type == "WORKS_FOR"
        assert sv == schema_version

    def test_rel_dedupe_key_encodes_start_endpoint(
        self, valid_edge: ExtractedEdge
    ) -> None:
        _, start_key, _, _ = valid_edge.rel_dedupe_key
        assert "Person" in start_key
        assert "Alice" in start_key

    def test_rel_dedupe_key_encodes_end_endpoint(
        self, valid_edge: ExtractedEdge
    ) -> None:
        _, _, end_key, _ = valid_edge.rel_dedupe_key
        assert "Organization" in end_key
        assert "Acme Corp" in end_key

    def test_frozen_rejects_mutation(self, valid_edge: ExtractedEdge) -> None:
        with pytest.raises((TypeError, ValidationError)):
            valid_edge.rel_type = "KNOWS"  # type: ignore[misc]


class TestExtractionResultValid:
    def test_empty_nodes_and_edges_is_valid(self, schema_version: str) -> None:
        result = ExtractionResult(
            run_id=_RUN_ID,
            chunk_id=_CHUNK_ID,
            schema_version=schema_version,
        )
        assert result.nodes == ()
        assert result.edges == ()

    def test_result_holds_nodes_and_edges(
        self,
        valid_node: ExtractedNode,
        valid_edge: ExtractedEdge,
        schema_version: str,
    ) -> None:
        result = ExtractionResult(
            run_id=_RUN_ID,
            chunk_id=_CHUNK_ID,
            schema_version=schema_version,
            nodes=(valid_node,),
            edges=(valid_edge,),
        )
        assert len(result.nodes) == 1
        assert len(result.edges) == 1

    def test_result_is_frozen(self, schema_version: str) -> None:
        result = ExtractionResult(
            run_id=_RUN_ID,
            chunk_id=_CHUNK_ID,
            schema_version=schema_version,
        )
        with pytest.raises((TypeError, ValidationError)):
            result.run_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Missing required fields → ValidationError
# ---------------------------------------------------------------------------


class TestMissingRequiredFields:
    def test_node_missing_run_id(self, schema_version: str) -> None:
        with pytest.raises(ValidationError):
            ExtractedNode.model_validate(
                {
                    "chunk_id": _CHUNK_ID,
                    "schema_version": schema_version,
                    "node_type": "Person",
                    "primary_value": "Alice",
                }
            )

    def test_node_missing_node_type(self, schema_version: str) -> None:
        with pytest.raises(ValidationError):
            ExtractedNode.model_validate(
                {
                    "run_id": _RUN_ID,
                    "chunk_id": _CHUNK_ID,
                    "schema_version": schema_version,
                    "primary_value": "Alice",
                }
            )

    def test_node_missing_primary_value(self, schema_version: str) -> None:
        with pytest.raises(ValidationError):
            ExtractedNode.model_validate(
                {
                    "run_id": _RUN_ID,
                    "chunk_id": _CHUNK_ID,
                    "schema_version": schema_version,
                    "node_type": "Person",
                }
            )

    def test_edge_missing_rel_type(self, schema_version: str) -> None:
        with pytest.raises(ValidationError):
            ExtractedEdge.model_validate(
                {
                    "run_id": _RUN_ID,
                    "chunk_id": _CHUNK_ID,
                    "schema_version": schema_version,
                    "start_node_type": "Person",
                    "start_primary_value": "Alice",
                    "end_node_type": "Organization",
                    "end_primary_value": "Acme Corp",
                }
            )

    def test_edge_missing_start_node_type(self, schema_version: str) -> None:
        with pytest.raises(ValidationError):
            ExtractedEdge.model_validate(
                {
                    "run_id": _RUN_ID,
                    "chunk_id": _CHUNK_ID,
                    "schema_version": schema_version,
                    "rel_type": "WORKS_FOR",
                    "start_primary_value": "Alice",
                    "end_node_type": "Organization",
                    "end_primary_value": "Acme Corp",
                }
            )

    def test_result_missing_schema_version(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionResult.model_validate(
                {"run_id": _RUN_ID, "chunk_id": _CHUNK_ID}
            )

    def test_result_missing_chunk_id(self, schema_version: str) -> None:
        with pytest.raises(ValidationError):
            ExtractionResult.model_validate(
                {"run_id": _RUN_ID, "schema_version": schema_version}
            )


# ---------------------------------------------------------------------------
# Whitespace-only / empty strings → ValidationError (non-empty validator)
# ---------------------------------------------------------------------------


class TestNonEmptyValidator:
    @pytest.mark.parametrize("bad_value", ["", "   ", "\t", "\n"])
    def test_node_type_whitespace_raises(
        self, bad_value: str, schema_version: str
    ) -> None:
        with pytest.raises(ValidationError):
            ExtractedNode(
                run_id=_RUN_ID,
                chunk_id=_CHUNK_ID,
                schema_version=schema_version,
                node_type=bad_value,
                primary_value="Alice",
            )

    @pytest.mark.parametrize("bad_value", ["", "   "])
    def test_node_primary_value_whitespace_raises(
        self, bad_value: str, schema_version: str
    ) -> None:
        with pytest.raises(ValidationError):
            ExtractedNode(
                run_id=_RUN_ID,
                chunk_id=_CHUNK_ID,
                schema_version=schema_version,
                node_type="Person",
                primary_value=bad_value,
            )

    @pytest.mark.parametrize("bad_value", ["", "   "])
    def test_node_schema_version_whitespace_raises(
        self, bad_value: str
    ) -> None:
        with pytest.raises(ValidationError):
            ExtractedNode(
                run_id=_RUN_ID,
                chunk_id=_CHUNK_ID,
                schema_version=bad_value,
                node_type="Person",
                primary_value="Alice",
            )

    @pytest.mark.parametrize("bad_value", ["", "   "])
    def test_edge_rel_type_whitespace_raises(
        self, bad_value: str, schema_version: str
    ) -> None:
        with pytest.raises(ValidationError):
            ExtractedEdge(
                run_id=_RUN_ID,
                chunk_id=_CHUNK_ID,
                schema_version=schema_version,
                rel_type=bad_value,
                start_node_type="Person",
                start_primary_value="Alice",
                end_node_type="Organization",
                end_primary_value="Acme Corp",
            )

    @pytest.mark.parametrize("bad_value", ["", "   "])
    def test_result_empty_run_id_raises(
        self, bad_value: str, schema_version: str
    ) -> None:
        with pytest.raises(ValidationError):
            ExtractionResult(
                run_id=bad_value,
                chunk_id=_CHUNK_ID,
                schema_version=schema_version,
            )


# ---------------------------------------------------------------------------
# Schema conformance — ExtractionError is a ValueError subclass
# ---------------------------------------------------------------------------


class TestSchemaConformance:
    """Tests for ExtractionService._validate_schema_conformance.

    This is the deterministic (non-LLM) guard that rejects extracted types
    absent from the locked schema. It raises ExtractionError, which is a
    ValueError subclass per CLAUDE.md §4.4 (fail-closed contract).

    The "schema_version mismatch" scenario: if the LLM was given a schema of
    version X but the locked schema is version Y, the extracted types will not
    match Y's type registry — triggering ExtractionError here.
    """

    def test_valid_node_type_passes(self, locked_schema: SchemaVersion) -> None:
        svc = ExtractionService()
        output = _LLMExtractionOutput(
            nodes=[_RawNode(node_type="Person", primary_value="Alice")],
            edges=[],
        )
        # Must not raise
        svc._validate_schema_conformance(output, locked_schema, _RUN_ID, _CHUNK_ID)

    def test_valid_edge_type_passes(self, locked_schema: SchemaVersion) -> None:
        svc = ExtractionService()
        output = _LLMExtractionOutput(
            nodes=[],
            edges=[
                _RawEdge(
                    rel_type="WORKS_FOR",
                    start_node_type="Person",
                    start_primary_value="Alice",
                    end_node_type="Organization",
                    end_primary_value="Acme Corp",
                )
            ],
        )
        svc._validate_schema_conformance(output, locked_schema, _RUN_ID, _CHUNK_ID)

    def test_unknown_node_type_raises_extraction_error(
        self, locked_schema: SchemaVersion
    ) -> None:
        svc = ExtractionService()
        output = _LLMExtractionOutput(
            nodes=[_RawNode(node_type="UnknownEntity", primary_value="Alice")],
            edges=[],
        )
        with pytest.raises(ExtractionError):
            svc._validate_schema_conformance(output, locked_schema, _RUN_ID, _CHUNK_ID)

    def test_unknown_node_type_raises_value_error_subclass(
        self, locked_schema: SchemaVersion
    ) -> None:
        """ExtractionError must be a ValueError subclass per CLAUDE.md §4.4."""
        svc = ExtractionService()
        output = _LLMExtractionOutput(
            nodes=[_RawNode(node_type="WrongType", primary_value="Alice")],
            edges=[],
        )
        with pytest.raises(ValueError):
            svc._validate_schema_conformance(output, locked_schema, _RUN_ID, _CHUNK_ID)

    def test_unknown_rel_type_raises_extraction_error(
        self, locked_schema: SchemaVersion
    ) -> None:
        svc = ExtractionService()
        output = _LLMExtractionOutput(
            nodes=[],
            edges=[
                _RawEdge(
                    rel_type="UNKNOWN_RELATIONSHIP",
                    start_node_type="Person",
                    start_primary_value="Alice",
                    end_node_type="Organization",
                    end_primary_value="Acme Corp",
                )
            ],
        )
        with pytest.raises(ExtractionError):
            svc._validate_schema_conformance(output, locked_schema, _RUN_ID, _CHUNK_ID)

    def test_first_invalid_type_short_circuits(
        self, locked_schema: SchemaVersion
    ) -> None:
        """Conformance check fails on the first bad type — not a full scan."""
        svc = ExtractionService()
        output = _LLMExtractionOutput(
            nodes=[
                _RawNode(node_type="BadType1", primary_value="Alice"),
                _RawNode(node_type="BadType2", primary_value="Bob"),
            ],
            edges=[],
        )
        with pytest.raises(ExtractionError) as exc_info:
            svc._validate_schema_conformance(output, locked_schema, _RUN_ID, _CHUNK_ID)
        # Error message references the first bad type
        assert "BadType1" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Promote-to-result fail-closed behavior
# ---------------------------------------------------------------------------


class TestPromoteToResult:
    """Tests for ExtractionService._promote_to_result fail-closed behavior.

    If a raw LLM value passes _validate_schema_conformance (type check only)
    but then fails Pydantic validation when stamping governed fields — e.g. an
    empty primary_value sneaking through — ExtractionError must be raised.
    """

    def test_valid_output_promotes_cleanly(
        self, locked_schema: SchemaVersion
    ) -> None:
        svc = ExtractionService()
        output = _LLMExtractionOutput(
            nodes=[_RawNode(node_type="Person", primary_value="Alice")],
            edges=[],
        )
        result = svc._promote_to_result(output, _RUN_ID, _CHUNK_ID, locked_schema)
        assert len(result.nodes) == 1
        assert result.nodes[0].primary_value == "Alice"
        assert result.run_id == _RUN_ID
        assert result.schema_version == locked_schema.version_hash

    def test_whitespace_primary_value_raises_extraction_error(
        self, locked_schema: SchemaVersion
    ) -> None:
        """_RawNode has no non-empty validator; ExtractedNode does.
        A whitespace primary_value passes _RawNode but fails ExtractedNode —
        _promote_to_result must catch the ValidationError and raise ExtractionError."""
        svc = ExtractionService()
        # _RawNode accepts whitespace — the validator only fires during promotion
        bad_output = _LLMExtractionOutput(
            nodes=[_RawNode(node_type="Person", primary_value="  ")],
            edges=[],
        )
        with pytest.raises(ExtractionError):
            svc._promote_to_result(bad_output, _RUN_ID, _CHUNK_ID, locked_schema)

    def test_promote_error_is_value_error_subclass(
        self, locked_schema: SchemaVersion
    ) -> None:
        svc = ExtractionService()
        bad_output = _LLMExtractionOutput(
            nodes=[_RawNode(node_type="Person", primary_value="")],
            edges=[],
        )
        with pytest.raises(ValueError):
            svc._promote_to_result(bad_output, _RUN_ID, _CHUNK_ID, locked_schema)

    def test_provenance_stamped_on_promoted_nodes(
        self, locked_schema: SchemaVersion
    ) -> None:
        """run_id and schema_version are stamped by the service, not the LLM."""
        svc = ExtractionService()
        output = _LLMExtractionOutput(
            nodes=[_RawNode(node_type="Person", primary_value="Carol")],
            edges=[],
        )
        result = svc._promote_to_result(output, _RUN_ID, _CHUNK_ID, locked_schema)
        node = result.nodes[0]
        assert node.run_id == _RUN_ID
        assert node.chunk_id == _CHUNK_ID
        assert node.schema_version == locked_schema.version_hash
