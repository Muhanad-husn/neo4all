"""
tests/unit/test_structural_anomaly.py — StructuralAnomalyDetector unit tests (SPEC-05 file 7e).

No network, no LLM, no Neo4j.  All graph data built from fixture JSON.

Four sub-detectors are exercised:
  orphan_node           — degree-zero nodes in OrphanResult
  degree_outlier        — total degree exceeds mean + 3σ (requires >= 3 DegreeRecords)
  missing_provenance    — blank schema_version on nodes or rels
  qualifier_missing     — node type defines a qualifier property that is absent/empty

Fixture has 10 normal Person nodes (degree=1 each) + 1 hub Organization (degree=10).
With 11 degree records, threshold ≈ 9.58 → hub (10) is an outlier.

Coverage:
  - All four sub-detectors fire on fixture graph
  - Below MIN_NODES_FOR_STATS → no degree outlier (graceful empty return)
  - Uniform degree (std_dev=0) → no degree outlier
  - Schema absent → qualifier_missing sub-detector skipped
  - No qualifier defined in schema → qualifier_missing sub-detector produces no candidates
  - Fail-closed: empty run_id raises ValidationError
"""

from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import ValidationError

from api.graph.reader_models import (
    DegreeListResult,
    DegreeRecord,
    GraphNodeRecord,
    GraphRelRecord,
    NodeListResult,
    OrphanResult,
    RelListResult,
)
from api.models.candidate import CandidateType
from api.schema.models import EdgeTypeDef, NodeTypeDef, SchemaVersion
from api.services.curation.candidates import MIN_NODES_FOR_STATS, StructuralAnomalyDetector

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "candidate_detection"
_RUN_ID = "unit-test-run-curation-0005"
_SV = "sv_fixture_abc123def456"
_DETECTOR = StructuralAnomalyDetector()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load() -> dict:
    return json.loads((_FIXTURES / "structural_anomaly.json").read_text(encoding="utf-8"))


def _build_nodes(raw: list[dict]) -> NodeListResult:
    return NodeListResult(nodes=tuple(GraphNodeRecord(**n) for n in raw))


def _build_rels(raw: list[dict]) -> RelListResult:
    return RelListResult(rels=tuple(GraphRelRecord(**r) for r in raw))


def _build_orphans(raw: dict) -> OrphanResult:
    return OrphanResult(orphan_dedupe_keys=tuple(raw["orphan_dedupe_keys"]))


def _build_degrees(raw: dict) -> DegreeListResult:
    return DegreeListResult(
        degrees=tuple(DegreeRecord(**d) for d in raw["degrees"])
    )


def _build_schema(fx: dict) -> SchemaVersion:
    sd = fx["schema"]
    return SchemaVersion(
        run_id=sd["run_id"],
        nodes=tuple(NodeTypeDef(**n) for n in sd["nodes"]),
        edges=tuple(EdgeTypeDef(**e) for e in sd["edges"]),
    )


def _fixture_inputs() -> tuple[NodeListResult, RelListResult, OrphanResult, DegreeListResult, SchemaVersion]:
    fx = _load()
    return (
        _build_nodes(fx["nodes"]),
        _build_rels(fx["rels"]),
        _build_orphans(fx["orphans_result"]),
        _build_degrees(fx["degrees_result"]),
        _build_schema(fx),
    )


def _by_method(candidates: list, method: str) -> list:
    return [c for c in candidates if c.detection_method == method]


# ---------------------------------------------------------------------------
# Tests — all sub-detectors fire on fixture
# ---------------------------------------------------------------------------


def test_orphan_candidate_detected() -> None:
    """orphan_pete has no edges → orphan_node candidate produced."""
    nodes, rels, orphans, degrees, schema = _fixture_inputs()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, rels, orphans, degrees, schema)

    orphan_candidates = _by_method(candidates, "orphan_node")
    assert len(orphan_candidates) == 1

    c = orphan_candidates[0]
    assert c.candidate_type == CandidateType.structural_anomaly
    assert "Person::orphan_pete::sv_fixture" in c.involved_element_refs


def test_degree_outlier_hub_detected() -> None:
    """hub_org (degree=10) exceeds the 3-sigma threshold computed from 11 records → detected."""
    nodes, rels, orphans, degrees, schema = _fixture_inputs()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, rels, orphans, degrees, schema)

    outlier_candidates = _by_method(candidates, "degree_outlier")
    assert len(outlier_candidates) == 1

    c = outlier_candidates[0]
    assert "Organization::hub_org::sv_fixture" in c.involved_element_refs
    assert c.collision_context["total_degree"] == 10


def test_degree_outlier_hub_exceeds_threshold() -> None:
    """Verify the hub total_degree > threshold stored in collision_context."""
    nodes, rels, orphans, degrees, schema = _fixture_inputs()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, rels, orphans, degrees, schema)

    c = _by_method(candidates, "degree_outlier")[0]
    assert c.collision_context["total_degree"] > c.collision_context["threshold"]


def test_missing_provenance_node_detected() -> None:
    """no_prov node has blank schema_version → missing_provenance_node candidate."""
    nodes, rels, orphans, degrees, schema = _fixture_inputs()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, rels, orphans, degrees, schema)

    prov_candidates = _by_method(candidates, "missing_provenance_node")
    assert len(prov_candidates) == 1

    c = prov_candidates[0]
    assert "Person::no_prov::sv_fixture" in c.involved_element_refs
    assert c.collision_context["missing_field"] == "schema_version"


def test_missing_provenance_rel_detected() -> None:
    """ghost_rel has blank schema_version → missing_provenance_rel candidate."""
    nodes, rels, orphans, degrees, schema = _fixture_inputs()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, rels, orphans, degrees, schema)

    rel_prov_candidates = _by_method(candidates, "missing_provenance_rel")
    assert len(rel_prov_candidates) == 1

    c = rel_prov_candidates[0]
    assert "ghost_rel::missing_prov" in c.involved_element_refs


def test_qualifier_missing_detected() -> None:
    """globex (Company) has no 'sector' property → qualifier_missing candidate."""
    nodes, rels, orphans, degrees, schema = _fixture_inputs()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, rels, orphans, degrees, schema)

    qual_candidates = _by_method(candidates, "qualifier_missing")
    assert len(qual_candidates) == 1

    c = qual_candidates[0]
    assert "Company::globex::sv_fixture" in c.involved_element_refs
    assert c.collision_context["qualifier_property"] == "sector"


def test_normal_nodes_not_flagged() -> None:
    """The 10 normal Person nodes with correct schema_version and no qualifier → no candidates."""
    nodes, rels, orphans, degrees, schema = _fixture_inputs()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, rels, orphans, degrees, schema)

    all_refs = {ref for c in candidates for ref in c.involved_element_refs}
    for i in range(1, 11):
        assert f"Person::n{i}::sv_fixture" not in all_refs


# ---------------------------------------------------------------------------
# Tests — degree outlier edge cases
# ---------------------------------------------------------------------------


def test_below_min_nodes_no_degree_outlier() -> None:
    """Fewer than MIN_NODES_FOR_STATS degree records → degree outlier skipped."""
    degrees = DegreeListResult(
        degrees=tuple(
            DegreeRecord(dedupe_key=f"X::n{i}::sv", in_degree=0, out_degree=i * 100)
            for i in range(MIN_NODES_FOR_STATS - 1)
        )
    )
    nodes = NodeListResult(nodes=())
    rels = RelListResult(rels=())
    orphans = OrphanResult(orphan_dedupe_keys=())
    candidates = _DETECTOR.detect(
        _RUN_ID, _SV, nodes, rels, orphans, degrees, schema=None
    )
    assert _by_method(candidates, "degree_outlier") == []


def test_uniform_degree_no_outlier() -> None:
    """All nodes have equal total degree → std_dev=0 → no degree outlier."""
    degrees = DegreeListResult(
        degrees=tuple(
            DegreeRecord(dedupe_key=f"X::n{i}::sv", in_degree=1, out_degree=1)
            for i in range(10)
        )
    )
    nodes = NodeListResult(nodes=())
    rels = RelListResult(rels=())
    orphans = OrphanResult(orphan_dedupe_keys=())
    candidates = _DETECTOR.detect(
        _RUN_ID, _SV, nodes, rels, orphans, degrees, schema=None
    )
    assert _by_method(candidates, "degree_outlier") == []


# ---------------------------------------------------------------------------
# Tests — qualifier sub-detector edge cases
# ---------------------------------------------------------------------------


def test_no_schema_skips_qualifier_check() -> None:
    """schema=None → qualifier_missing sub-detector is skipped entirely."""
    nodes, rels, orphans, degrees, _ = _fixture_inputs()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, rels, orphans, degrees, schema=None)
    assert _by_method(candidates, "qualifier_missing") == []


def test_schema_with_no_qualifiers_produces_no_qualifier_candidates() -> None:
    """Schema where no NodeTypeDef has qualifier → qualifier sub-detector returns empty."""
    schema_no_qual = SchemaVersion(
        run_id=_RUN_ID,
        nodes=(
            NodeTypeDef(node_class="Entity", type="Person", primary_property="name"),
        ),
        edges=(),
    )
    nodes, rels, orphans, degrees, _ = _fixture_inputs()
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, rels, orphans, degrees, schema_no_qual)
    assert _by_method(candidates, "qualifier_missing") == []


def test_qualifier_empty_string_also_detected() -> None:
    """A node with qualifier property present but set to empty string is also flagged."""
    schema = SchemaVersion(
        run_id=_RUN_ID,
        nodes=(NodeTypeDef(node_class="Entity", type="Company",
                           primary_property="name", qualifier="sector"),),
        edges=(),
    )
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Company::empty_sector::sv", node_type="Company",
                            run_id=_RUN_ID, schema_version=_SV,
                            properties={"name": "EmptyCo", "sector": ""}),
        )
    )
    orphans = OrphanResult(orphan_dedupe_keys=())
    rels = RelListResult(rels=())
    degrees = DegreeListResult(degrees=())
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes, rels, orphans, degrees, schema)
    assert len(_by_method(candidates, "qualifier_missing")) == 1


# ---------------------------------------------------------------------------
# Tests — fail-closed
# ---------------------------------------------------------------------------


def test_fail_closed_empty_run_id_on_orphan() -> None:
    """Empty run_id raises ValidationError when constructing orphan candidate."""
    orphans = OrphanResult(orphan_dedupe_keys=("some::dedupe::key",))
    nodes = NodeListResult(nodes=())
    rels = RelListResult(rels=())
    degrees = DegreeListResult(degrees=())
    with pytest.raises(ValidationError):
        _DETECTOR.detect("", _SV, nodes, rels, orphans, degrees, schema=None)
