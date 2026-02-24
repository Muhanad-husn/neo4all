"""
tests/unit/test_probable_dup.py — ProbableDuplicateDetector unit tests (SPEC-05 file 7c).

No network, no LLM, no Neo4j.  Schema is built from fixture data; nodes fed directly.

The Jaro-Winkler implementation is pure Python inside candidates.py; these tests
treat it as a black-box and verify observable behaviour:
  - Pairs with JW >= 0.90 (threshold) are detected.
  - Pairs clearly below 0.90 are not detected.
  - Threshold boundary: a pair with JW just below 0.90 produces no candidate.
  - context_jaccard = 0.0 when no rels provided.
  - collision_context jaro_winkler value >= JW_THRESHOLD for detected pairs.
  - Nodes with unknown type (not in schema) → skipped, no error.
  - Fail-closed: empty run_id raises ValidationError.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import ValidationError

from api.graph.reader_models import GraphNodeRecord, GraphRelRecord, NodeListResult, RelListResult
from api.models.candidate import CandidateLane, CandidateType, Severity
from api.schema.models import EdgeTypeDef, NodeTypeDef, SchemaVersion
from api.services.curation.candidates import JW_THRESHOLD, ProbableDuplicateDetector

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "candidate_detection"
_RUN_ID = "unit-test-run-curation-0003"
_SV = "sv_fixture_abc123def456"
_DETECTOR = ProbableDuplicateDetector()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load() -> dict:
    return json.loads((_FIXTURES / "probable_dup.json").read_text(encoding="utf-8"))


def _build_schema(fx: dict) -> SchemaVersion:
    sd = fx["schema"]
    return SchemaVersion(
        run_id=sd["run_id"],
        nodes=tuple(NodeTypeDef(**n) for n in sd["nodes"]),
        edges=tuple(EdgeTypeDef(**e) for e in sd["edges"]),
    )


def _build_nodes(raw: list[dict]) -> NodeListResult:
    return NodeListResult(nodes=tuple(GraphNodeRecord(**n) for n in raw))


def _fixture_nodes() -> NodeListResult:
    return _build_nodes(_load()["nodes"])


def _fixture_schema() -> SchemaVersion:
    return _build_schema(_load())


# ---------------------------------------------------------------------------
# Tests — detection on fixture graph
# ---------------------------------------------------------------------------


def test_exactly_one_candidate_detected() -> None:
    """'Jonathan Jones' / 'Jonathon Jones' pair → exactly one candidate; 'Alice Smith' silent."""
    candidates = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes(), _fixture_schema())
    assert len(candidates) == 1


def test_candidate_type_and_lane() -> None:
    """Detected candidate is classified as probable_duplicate in the node lane."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes(), _fixture_schema())[0]
    assert c.candidate_type == CandidateType.probable_duplicate
    assert c.candidate_lane == CandidateLane.node


def test_severity_is_medium() -> None:
    """Probable duplicates are medium severity per SPEC-05 S-05.1."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes(), _fixture_schema())[0]
    assert c.severity == Severity.medium


def test_detection_method_encodes_threshold() -> None:
    """detection_method string encodes the JW threshold used."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes(), _fixture_schema())[0]
    assert c.detection_method == ProbableDuplicateDetector.DETECTION_METHOD
    assert "0.90" in c.detection_method


def test_involved_refs_are_both_similar_nodes() -> None:
    """involved_element_refs contains exactly the two similar nodes from the fixture."""
    fx = _load()
    expected_refs = set(fx["expected_candidates"][0]["involved_element_refs"])
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes(), _fixture_schema())[0]
    assert set(c.involved_element_refs) == expected_refs


def test_jaro_winkler_in_context_above_threshold() -> None:
    """collision_context.jaro_winkler value is >= JW_THRESHOLD for the detected pair."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes(), _fixture_schema())[0]
    assert c.collision_context["jaro_winkler"] >= JW_THRESHOLD


def test_context_jaccard_zero_when_no_rels() -> None:
    """Without rels argument, context_jaccard = 0.0 (no adjacency to compare)."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes(), _fixture_schema(), rels=None)[0]
    assert c.collision_context["context_jaccard"] == 0.0


def test_primary_property_recorded_in_context() -> None:
    """collision_context records the primary_property name and the compared values."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes(), _fixture_schema())[0]
    assert c.collision_context["primary_property"] == "name"
    # Compared values are lower-cased and stripped
    assert "jonathan jones" in c.collision_context["value_a"] or \
           "jonathan jones" in c.collision_context["value_b"]
    assert "jonathon jones" in c.collision_context["value_a"] or \
           "jonathon jones" in c.collision_context["value_b"]


# ---------------------------------------------------------------------------
# Tests — below-threshold pair not detected
# ---------------------------------------------------------------------------


def _person_schema() -> SchemaVersion:
    return SchemaVersion(
        run_id=_RUN_ID,
        nodes=(NodeTypeDef(node_class="Entity", type="Person", primary_property="name"),),
        edges=(),
    )


def test_clearly_different_names_not_detected() -> None:
    """'Alice' and 'Bob' have JW well below 0.90 → no candidates."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Person::alice::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "Alice"}),
            GraphNodeRecord(dedupe_key="Person::bob::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "Bob"}),
        )
    )
    assert _DETECTOR.detect(_RUN_ID, _SV, nodes, _person_schema()) == []


def test_exact_same_name_different_keys_is_detected() -> None:
    """Two nodes with identical primary property values (JW=1.0) but different keys → detected."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Person::alice_v1::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "Alice"}),
            GraphNodeRecord(dedupe_key="Person::alice_v2::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "alice"}),
        )
    )
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, _person_schema())
    # "alice" == "alice" (case-insensitive comparison) → JW=1.0 → detected
    assert len(candidates) == 1
    assert candidates[0].collision_context["jaro_winkler"] == 1.0


def test_single_node_of_type_no_pairs_formed() -> None:
    """Only one node of a given type → no pairs to compare → no candidates."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Person::solo::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "Solo"}),
        )
    )
    assert _DETECTOR.detect(_RUN_ID, _SV, nodes, _person_schema()) == []


def test_node_type_not_in_schema_skipped() -> None:
    """Nodes whose type is absent from the schema are silently skipped."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="UnknownType::x::sv", node_type="UnknownType",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "X"}),
            GraphNodeRecord(dedupe_key="UnknownType::y::sv", node_type="UnknownType",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "Y"}),
        )
    )
    # Schema has no 'UnknownType' → no primary_property → skipped
    assert _DETECTOR.detect(_RUN_ID, _SV, nodes, _person_schema()) == []


def test_node_missing_primary_property_skipped() -> None:
    """Nodes with no value for their type's primary_property are skipped (empty string)."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Person::noprop_a::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphNodeRecord(dedupe_key="Person::noprop_b::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    # Missing "name" property → _primary_value returns "" → skipped
    assert _DETECTOR.detect(_RUN_ID, _SV, nodes, _person_schema()) == []


# ---------------------------------------------------------------------------
# Tests — context Jaccard with rels
# ---------------------------------------------------------------------------


def test_context_jaccard_nonzero_for_shared_neighbors() -> None:
    """Nodes with overlapping neighbor sets produce context_jaccard > 0."""
    schema = _person_schema()
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Person::a::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "Alice"}),
            GraphNodeRecord(dedupe_key="Person::b::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "alice"}),
        )
    )
    # Both a and b connect to the same neighbor "shared::sv"
    rels = RelListResult(
        rels=(
            GraphRelRecord(dedupe_key="KNOWS::a::shared::sv", rel_type="KNOWS",
                           start_dedupe_key="Person::a::sv", end_dedupe_key="shared::sv",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphRelRecord(dedupe_key="KNOWS::b::shared::sv", rel_type="KNOWS",
                           start_dedupe_key="Person::b::sv", end_dedupe_key="shared::sv",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, schema, rels=rels)
    assert len(candidates) == 1
    assert candidates[0].collision_context["context_jaccard"] == 1.0  # identical neighbor sets


# ---------------------------------------------------------------------------
# Tests — fail-closed
# ---------------------------------------------------------------------------


def test_fail_closed_empty_run_id() -> None:
    """Empty run_id raises ValidationError when Candidate is constructed."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Person::x::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "alice"}),
            GraphNodeRecord(dedupe_key="Person::y::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "alice"}),
        )
    )
    with pytest.raises(ValidationError):
        _DETECTOR.detect("", _SV, nodes, _person_schema())
