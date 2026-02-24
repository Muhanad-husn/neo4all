"""
tests/unit/test_exact_rel_dup.py — ExactRelDuplicateDetector unit tests (SPEC-05 file 7b).

No network, no LLM, no Neo4j.  Graph reader is replaced by RelListResult instances
built directly from fixture data fed into the detector.

Coverage:
  - Fixture graph with known duplicate rels → exactly one candidate produced
  - Involved element refs and collision_context match fixture expectations
  - All-unique rels → empty result
  - Empty RelListResult → empty result
  - Three identical rel dedupe_keys → one candidate with count 3
  - Fail-closed: empty run_id raises ValidationError
  - Fail-closed: empty schema_version raises ValidationError
"""

from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import ValidationError

from api.graph.reader_models import GraphRelRecord, RelListResult
from api.models.candidate import CandidateLane, CandidateType, Severity
from api.services.curation.candidates import ExactRelDuplicateDetector

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "candidate_detection"
_RUN_ID = "unit-test-run-curation-0002"
_SV = "sv_fixture_abc123def456"
_DETECTOR = ExactRelDuplicateDetector()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load() -> dict:
    return json.loads((_FIXTURES / "exact_rel_dup.json").read_text(encoding="utf-8"))


def _build_rels(raw: list[dict]) -> RelListResult:
    return RelListResult(rels=tuple(GraphRelRecord(**r) for r in raw))


def _fixture_rels() -> RelListResult:
    """Two rels sharing a dedupe_key + one unique rel (from fixture)."""
    return _build_rels(_load()["rels"])


def _dup_rel_pair() -> RelListResult:
    """Minimal two-rel duplicate pair to trigger Candidate construction."""
    return RelListResult(
        rels=(
            GraphRelRecord(dedupe_key="TYPE::a::b::sv", rel_type="TYPE",
                           start_dedupe_key="a", end_dedupe_key="b",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphRelRecord(dedupe_key="TYPE::a::b::sv", rel_type="TYPE",
                           start_dedupe_key="a", end_dedupe_key="b",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )


# ---------------------------------------------------------------------------
# Tests — detection on fixture graph
# ---------------------------------------------------------------------------


def test_exactly_one_candidate_detected() -> None:
    """Fixture has one duplicate rel group → detector produces exactly one candidate."""
    candidates = _DETECTOR.detect(_RUN_ID, _SV, _fixture_rels())
    assert len(candidates) == 1


def test_candidate_type_and_lane() -> None:
    """Detected candidate is classified as exact_rel_duplicate in the relationship lane."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_rels())[0]
    assert c.candidate_type == CandidateType.exact_rel_duplicate
    assert c.candidate_lane == CandidateLane.relationship


def test_severity_is_high() -> None:
    """Exact rel duplicates are high severity per SPEC-05 S-05.1."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_rels())[0]
    assert c.severity == Severity.high


def test_detection_method_matches_constant() -> None:
    """detection_method matches the detector's own class constant."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_rels())[0]
    assert c.detection_method == ExactRelDuplicateDetector.DETECTION_METHOD


def test_involved_refs_match_fixture() -> None:
    """involved_element_refs references the expected duplicate rel dedupe_key."""
    fx = _load()
    expected_refs = set(fx["expected_candidates"][0]["involved_element_refs"])
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_rels())[0]
    assert set(c.involved_element_refs) == expected_refs


def test_collision_context_duplicate_count() -> None:
    """collision_context.duplicate_count reflects the actual number of copies."""
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_rels())[0]
    assert c.collision_context["duplicate_count"] == 2


def test_collision_context_rel_metadata() -> None:
    """collision_context records rel_type, start/end dedupe_keys, and the duplicate key."""
    fx = _load()
    expected_ctx = fx["expected_candidates"][0]["collision_context"]
    c = _DETECTOR.detect(_RUN_ID, _SV, _fixture_rels())[0]
    assert c.collision_context["rel_type"] == expected_ctx["rel_type"]
    assert c.collision_context["dedupe_key"] == expected_ctx["dedupe_key"]
    assert c.collision_context["start_dedupe_key"] == expected_ctx["start_dedupe_key"]
    assert c.collision_context["end_dedupe_key"] == expected_ctx["end_dedupe_key"]


# ---------------------------------------------------------------------------
# Tests — no-duplicate graphs
# ---------------------------------------------------------------------------


def test_all_unique_keys_returns_empty() -> None:
    """All distinct rel dedupe_keys → no candidates."""
    rels = RelListResult(
        rels=(
            GraphRelRecord(dedupe_key="WORKS_FOR::a::b::sv", rel_type="WORKS_FOR",
                           start_dedupe_key="a", end_dedupe_key="b",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphRelRecord(dedupe_key="KNOWS::b::c::sv", rel_type="KNOWS",
                           start_dedupe_key="b", end_dedupe_key="c",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    assert _DETECTOR.detect(_RUN_ID, _SV, rels) == []


def test_empty_rel_list_returns_empty() -> None:
    """Empty RelListResult → no candidates, no error."""
    assert _DETECTOR.detect(_RUN_ID, _SV, RelListResult(rels=())) == []


# ---------------------------------------------------------------------------
# Tests — triple duplicate
# ---------------------------------------------------------------------------


def test_triple_duplicate_single_candidate_count_three() -> None:
    """Three rels sharing the same dedupe_key → one candidate, count=3."""
    rels = RelListResult(
        rels=(
            GraphRelRecord(dedupe_key="KNOWS::x::y::sv", rel_type="KNOWS",
                           start_dedupe_key="x", end_dedupe_key="y",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphRelRecord(dedupe_key="KNOWS::x::y::sv", rel_type="KNOWS",
                           start_dedupe_key="x", end_dedupe_key="y",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphRelRecord(dedupe_key="KNOWS::x::y::sv", rel_type="KNOWS",
                           start_dedupe_key="x", end_dedupe_key="y",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    candidates = _DETECTOR.detect(_RUN_ID, _SV, rels)
    assert len(candidates) == 1
    assert candidates[0].collision_context["duplicate_count"] == 3


def test_two_independent_duplicate_groups() -> None:
    """Two separate duplicate rel groups → two candidates."""
    rels = RelListResult(
        rels=(
            GraphRelRecord(dedupe_key="TYPE_A::x::y::sv", rel_type="TYPE_A",
                           start_dedupe_key="x", end_dedupe_key="y",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphRelRecord(dedupe_key="TYPE_A::x::y::sv", rel_type="TYPE_A",
                           start_dedupe_key="x", end_dedupe_key="y",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphRelRecord(dedupe_key="TYPE_B::p::q::sv", rel_type="TYPE_B",
                           start_dedupe_key="p", end_dedupe_key="q",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
            GraphRelRecord(dedupe_key="TYPE_B::p::q::sv", rel_type="TYPE_B",
                           start_dedupe_key="p", end_dedupe_key="q",
                           run_id=_RUN_ID, schema_version=_SV, properties={}),
        )
    )
    candidates = _DETECTOR.detect(_RUN_ID, _SV, rels)
    assert len(candidates) == 2


# ---------------------------------------------------------------------------
# Tests — fail-closed: invalid inputs raise, not silently pass
# ---------------------------------------------------------------------------


def test_fail_closed_empty_run_id() -> None:
    """Empty run_id passed to detect() raises ValidationError via Candidate model."""
    with pytest.raises(ValidationError):
        _DETECTOR.detect("", _SV, _dup_rel_pair())


def test_fail_closed_empty_schema_version() -> None:
    """Empty schema_version raises ValidationError via Candidate model."""
    with pytest.raises(ValidationError):
        _DETECTOR.detect(_RUN_ID, "", _dup_rel_pair())
