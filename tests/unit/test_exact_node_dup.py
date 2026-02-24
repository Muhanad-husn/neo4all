"""
tests/unit/test_exact_node_dup.py — ExactNodeDuplicateDetector unit tests (SPEC-05 file 7a).

No network, no LLM, no Neo4j.  Graph reader is replaced by NodeListResult instances
built directly from fixture data fed into the detector.

Coverage:
  - Fixture graph with known duplicates → exactly one candidate produced
  - Involved element refs and collision_context match fixture expectations
  - Same key, different node_type → NOT a duplicate (groups keyed by both fields)
  - All-unique graph → empty result
  - Empty NodeListResult → empty result
  - Three identical dedupe_keys → one candidate with count 3
  - Fail-closed: empty run_id raises ValidationError
  - Fail-closed: whitespace-only run_id raises ValidationError
  - Fail-closed: empty schema_version raises ValidationError
"""

from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import ValidationError

from api.graph.reader_models import GraphNodeRecord, NodeListResult
from api.models.candidate import CandidateLane, CandidateType, Severity
from api.services.curation.candidates import ExactNodeDuplicateDetector

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "candidate_detection"
_RUN_ID = "unit-test-run-curation-0001"
_SV = "sv_fixture_abc123def456"
_DETECTOR = ExactNodeDuplicateDetector()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load() -> dict:
    return json.loads((_FIXTURES / "exact_node_dup.json").read_text(encoding="utf-8"))


def _build_nodes(raw: list[dict]) -> NodeListResult:
    return NodeListResult(nodes=tuple(GraphNodeRecord(**n) for n in raw))


def _fixture_nodes() -> NodeListResult:
    """Two nodes sharing a dedupe_key + one unique node (from fixture)."""
    return _build_nodes(_load()["nodes"])


def _dup_pair() -> NodeListResult:
    """Minimal two-node duplicate pair to trigger Candidate construction."""
    return NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="X::a::sv", node_type="X",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphNodeRecord(dedupe_key="X::a::sv", node_type="X",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )


# ---------------------------------------------------------------------------
# Tests — detection on fixture graph
# ---------------------------------------------------------------------------


def test_exactly_one_candidate_detected() -> None:
    """Fixture has one duplicate group → detector produces exactly one candidate."""
    candidates = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes())
    assert len(candidates) == 1


def test_candidate_type_and_lane() -> None:
    """Detected candidate is classified as exact_node_duplicate in the node lane."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes())[0]
    assert c.candidate_type == CandidateType.exact_node_duplicate
    assert c.candidate_lane == CandidateLane.node


def test_severity_is_high() -> None:
    """Exact node duplicates are high severity per SPEC-05 S-05.1."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes())[0]
    assert c.severity == Severity.high


def test_detection_method_matches_constant() -> None:
    """detection_method matches the detector's own class constant."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes())[0]
    assert c.detection_method == ExactNodeDuplicateDetector.DETECTION_METHOD


def test_involved_refs_match_fixture() -> None:
    """involved_element_refs references the expected duplicate dedupe_key."""
    fx = _load()
    expected_refs = set(fx["expected_candidates"][0]["involved_element_refs"])
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes())[0]
    assert set(c.involved_element_refs) == expected_refs


def test_collision_context_duplicate_count() -> None:
    """collision_context.duplicate_count reflects the actual number of copies."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes())[0]
    assert c.collision_context["duplicate_count"] == 2


def test_collision_context_node_type_and_key() -> None:
    """collision_context records the node_type and the duplicated dedupe_key."""
    fx = _load()
    expected_ctx = fx["expected_candidates"][0]["collision_context"]
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_nodes())[0]
    assert c.collision_context["node_type"] == expected_ctx["node_type"]
    assert c.collision_context["dedupe_key"] == expected_ctx["dedupe_key"]


# ---------------------------------------------------------------------------
# Tests — no-duplicate graphs
# ---------------------------------------------------------------------------


def test_all_unique_keys_returns_empty() -> None:
    """All distinct dedupe_keys → no candidates."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Person::alice::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphNodeRecord(dedupe_key="Person::bob::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    assert _DETECTOR.detect(_RUN_ID, _SV, nodes) == []


def test_empty_node_list_returns_empty() -> None:
    """Empty NodeListResult → no candidates, no error."""
    assert _DETECTOR.detect(_RUN_ID, _SV, NodeListResult(nodes=())) == []


def test_same_key_different_node_type_no_candidate() -> None:
    """Same dedupe_key string but different node_type → two separate groups, no duplicate."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="shared::key::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphNodeRecord(dedupe_key="shared::key::sv", node_type="Organization",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    # Groups are keyed by (node_type, dedupe_key): each group has count 1 → no candidate.
    assert _DETECTOR.detect(_RUN_ID, _SV, nodes) == []


# ---------------------------------------------------------------------------
# Tests — triple duplicate
# ---------------------------------------------------------------------------


def test_triple_duplicate_single_candidate_count_three() -> None:
    """Three nodes sharing the same (node_type, dedupe_key) → one candidate, count=3."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Person::dup::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "A"}),
            GraphNodeRecord(dedupe_key="Person::dup::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "B"}),
            GraphNodeRecord(dedupe_key="Person::dup::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={"name": "C"}),
        )
    )
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes)
    assert len(candidates) == 1
    assert candidates[0].collision_context["duplicate_count"] == 3


def test_two_independent_duplicate_groups() -> None:
    """Two separate duplicate groups → two candidates, one per group."""
    nodes = NodeListResult(
        nodes=(
            GraphNodeRecord(dedupe_key="Person::alpha::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphNodeRecord(dedupe_key="Person::alpha::sv", node_type="Person",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphNodeRecord(dedupe_key="Organization::beta::sv", node_type="Organization",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphNodeRecord(dedupe_key="Organization::beta::sv", node_type="Organization",
                            run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    candidates = _DETECTOR.detect(_RUN_ID, _SV, nodes)
    assert len(candidates) == 2
    methods = {c.detection_method for c in candidates}
    assert methods == {ExactNodeDuplicateDetector.DETECTION_METHOD}


# ---------------------------------------------------------------------------
# Tests — fail-closed: invalid inputs raise, not silently pass
# ---------------------------------------------------------------------------


def test_fail_closed_empty_run_id() -> None:
    """Empty run_id passed to detect() raises ValidationError via Candidate model."""
    with pytest.raises(ValidationError):
        _DETECTOR.detect("", _SV, _dup_pair())


def test_fail_closed_whitespace_run_id() -> None:
    """Whitespace-only run_id raises ValidationError (non-empty strip check)."""
    with pytest.raises(ValidationError):
        _DETECTOR.detect("   ", _SV, _dup_pair())


def test_fail_closed_empty_schema_version() -> None:
    """Empty schema_version raises ValidationError via Candidate model."""
    with pytest.raises(ValidationError):
        _DETECTOR.detect(_RUN_ID, "", _dup_pair())
