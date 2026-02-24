"""
tests/unit/test_canonical_violation.py — CanonicalViolationDetector unit tests (SPEC-05 file 7d).

No network, no LLM, no Neo4j.  Graph and schema built from fixture data.

Sub-detectors:
  canonical_inverse_violation  — edge direction is reversed vs. schema definition
  canonical_direction_violation — edge node-types do not match any schema definition
    (including reversed)

Coverage:
  - Fixture: one inverse violation, one direction violation, one compliant rel
  - Compliant rel produces no candidate
  - Inverse violation candidate has correct detection_method and refs
  - Direction violation candidate has correct detection_method and refs
  - All-compliant graph → empty result
  - Unknown rel type (not in schema) treated as direction violation
  - Fail-closed: empty schema_version raises ValidationError
"""

from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import ValidationError

from api.graph.reader_models import GraphNodeRecord, GraphRelRecord, NodeListResult, RelListResult
from api.models.candidate import CandidateLane, CandidateType, Severity
from api.schema.models import EdgeTypeDef, NodeTypeDef, SchemaVersion
from api.services.curation.candidates import CanonicalViolationDetector

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "candidate_detection"
_RUN_ID = "unit-test-run-curation-0004"
_SV = "sv_fixture_abc123def456"
_DETECTOR = CanonicalViolationDetector()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load() -> dict:
    return json.loads((_FIXTURES / "canonical_violation.json").read_text(encoding="utf-8"))


def _build_nodes(raw: list[dict]) -> NodeListResult:
    return NodeListResult(nodes=tuple(GraphNodeRecord(**n) for n in raw))


def _build_rels(raw: list[dict]) -> RelListResult:
    return RelListResult(rels=tuple(GraphRelRecord(**r) for r in raw))


def _build_schema(fx: dict) -> SchemaVersion:
    sd = fx["schema"]
    return SchemaVersion(
        run_id=sd["run_id"],
        nodes=tuple(NodeTypeDef(**n) for n in sd["nodes"]),
        edges=tuple(EdgeTypeDef(**e) for e in sd["edges"]),
    )


def _fixture_data() -> tuple[NodeListResult, RelListResult, SchemaVersion]:
    fx = _load()
    return _build_nodes(fx["nodes"]), _build_rels(fx["rels"]), _build_schema(fx)


# ---------------------------------------------------------------------------
# Tests — detection on fixture graph
# ---------------------------------------------------------------------------


def test_exactly_two_candidates_detected() -> None:
    """Fixture has one inverse + one direction violation; one compliant rel → 2 candidates."""
    nodes, rels, schema = _fixture_data()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, rels, nodes, schema)
    assert len(candidates) == 2


def test_inverse_violation_candidate_present() -> None:
    """The backwards-stored WORKS_FOR rel is detected as canonical_inverse_violation."""
    fx = _load()
    nodes, rels, schema = _fixture_data()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, rels, nodes, schema)

    inverse = [c for c in candidates
               if c.detection_method == CanonicalViolationDetector.DETECTION_METHOD_INVERSE]
    assert len(inverse) == 1

    inv = inverse[0]
    assert inv.candidate_type == CandidateType.canonical_violation
    assert inv.candidate_lane == CandidateLane.relationship
    assert inv.severity == Severity.high

    expected_key = fx["expected_candidates"][0]["rel_dedupe_key"]
    assert expected_key in inv.involved_element_refs


def test_direction_violation_candidate_present() -> None:
    """The MENTORS rel from Org→Person is detected as canonical_direction_violation."""
    fx = _load()
    nodes, rels, schema = _fixture_data()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, rels, nodes, schema)

    direction = [c for c in candidates
                 if c.detection_method == CanonicalViolationDetector.DETECTION_METHOD_DIRECTION]
    assert len(direction) == 1

    dirv = direction[0]
    assert dirv.candidate_type == CandidateType.canonical_violation
    assert dirv.candidate_lane == CandidateLane.relationship
    assert dirv.severity == Severity.medium

    expected_key = fx["expected_candidates"][1]["rel_dedupe_key"]
    assert expected_key in dirv.involved_element_refs


def test_compliant_rel_produces_no_candidate() -> None:
    """The valid WORKS_FOR (Person→Organization) must not appear in any candidate's refs."""
    fx = _load()
    nodes, rels, schema = _fixture_data()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, rels, nodes, schema)

    compliant_key = fx["compliant_rel_dedupe_key"]
    all_refs = {ref for c in candidates for ref in c.involved_element_refs}
    assert compliant_key not in all_refs


def test_inverse_violation_context_records_swap() -> None:
    """collision_context for inverse violation records actual and expected node types."""
    nodes, rels, schema = _fixture_data()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, rels, nodes, schema)

    inv = next(c for c in candidates
               if c.detection_method == CanonicalViolationDetector.DETECTION_METHOD_INVERSE)
    ctx = inv.collision_context
    # Actual start is Organization, actual end is Person
    assert ctx["actual_start_type"] == "Organization"
    assert ctx["actual_end_type"] == "Person"
    # Expected is the canonical direction: Person→Organization
    assert ctx["expected_start_type"] == "Person"
    assert ctx["expected_end_type"] == "Organization"


# ---------------------------------------------------------------------------
# Tests — all-compliant graph
# ---------------------------------------------------------------------------


def _compliant_schema() -> SchemaVersion:
    return SchemaVersion(
        run_id=_RUN_ID,
        nodes=(
            NodeTypeDef(node_class="Entity", type="Person", primary_property="name"),
            NodeTypeDef(node_class="Entity", type="Organization", primary_property="name"),
        ),
        edges=(
            EdgeTypeDef(start_node_type="Person", end_node_type="Organization",
                        type="WORKS_FOR"),
        ),
    )


def test_all_compliant_graph_returns_empty() -> None:
    """A graph where every rel matches its schema edge definition → no candidates."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Person::alice::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphNodeRecord(dedupe_key="Organization::acme::sv", node_type="Organization",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    rels = RelListResult(
        rels=(
            GraphRelRecord(dedupe_key="WORKS_FOR::alice::acme::sv", rel_type="WORKS_FOR",
                           start_dedupe_key="Person::alice::sv",
                           end_dedupe_key="Organization::acme::sv",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    candidates = _DETECTOR.detect(_RUN_ID, _SV, rels, nodes, _compliant_schema())
    assert candidates == []


def test_empty_rels_returns_empty() -> None:
    """Empty RelListResult → no candidates, no error."""
    nodes = NodeListResult(nodes=())
    rels = RelListResult(rels=())
    assert _DETECTOR.detect(_RUN_ID, _SV, rels, nodes, _compliant_schema()) == []


# ---------------------------------------------------------------------------
# Tests — edge cases
# ---------------------------------------------------------------------------


def test_unknown_rel_type_produces_direction_violation() -> None:
    """A rel_type absent from the schema entirely → direction violation."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Person::x::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphNodeRecord(dedupe_key="Person::y::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    rels = RelListResult(
        rels=(
            GraphRelRecord(dedupe_key="PHANTOM::x::y::sv", rel_type="PHANTOM",
                           start_dedupe_key="Person::x::sv",
                           end_dedupe_key="Person::y::sv",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    candidates = _DETECTOR.detect(_RUN_ID, _SV, rels, nodes, _compliant_schema())
    assert len(candidates) == 1
    assert candidates[0].detection_method == CanonicalViolationDetector.DETECTION_METHOD_DIRECTION


def test_node_not_in_node_map_resolves_as_empty_type() -> None:
    """A rel referencing an unknown dedupe_key resolves to '' node_type (treated as mismatch)."""
    # Schema: WORKS_FOR Person→Organization; rel references nodes not in the nodes list.
    nodes = NodeListResult(nodes=())
    rels = RelListResult(
        rels=(
            GraphRelRecord(dedupe_key="WORKS_FOR::ghost_a::ghost_b::sv", rel_type="WORKS_FOR",
                           start_dedupe_key="ghost_a",
                           end_dedupe_key="ghost_b",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    # ''→'' does not match Person→Organization or reversed → direction violation
    candidates = _DETECTOR.detect(_RUN_ID, _SV, rels, nodes, _compliant_schema())
    assert len(candidates) == 1
    assert candidates[0].detection_method == CanonicalViolationDetector.DETECTION_METHOD_DIRECTION


# ---------------------------------------------------------------------------
# Tests — fail-closed
# ---------------------------------------------------------------------------


def test_fail_closed_empty_schema_version() -> None:
    """Empty schema_version raises ValidationError via Candidate model."""
    fx = _load()
    nodes, rels, schema = _fixture_data()
    with pytest.raises(ValidationError):
        _DETECTOR.detect(_RUN_ID, "", rels, nodes, schema)
