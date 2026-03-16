"""
tests/unit/test_structural.py — Structural recommendation tests.

Tests the deterministic structural recommendation logic for probable_duplicate,
canonical_violation, and other candidate types.

No network, no LLM, no Neo4j.
"""

from __future__ import annotations

from api.agents.models import StructuralRecommendation
from api.agents.structural import compute_structural_recommendation
from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-structural"
_SCHEMA_VERSION = "test_schema_v1_hash_0000000000000000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_probable_dup(
    val_a: str = "Engrid",
    val_b: str = "Ingrid Boss",
    jw: float = 0.92,
    tok: float = 0.80,
    cj: float = 0.50,
) -> Candidate:
    return Candidate(
        run_id=_RUN_ID,
        schema_version=_SCHEMA_VERSION,
        candidate_type=CandidateType.probable_duplicate,
        candidate_lane=CandidateLane.node,
        involved_element_refs=("node_a", "node_b"),
        severity=Severity.high,
        detection_method="jaro_winkler_0.9",
        collision_context={
            "value_a": val_a,
            "value_b": val_b,
            "jaro_winkler": jw,
            "token_overlap": tok,
            "context_jaccard": cj,
        },
    )


# ===========================================================================
# Probable duplicate tests
# ===========================================================================


class TestStructuralProbableDuplicate:
    """Tests for probable_duplicate recommendations."""

    def test_high_similarity_with_context_recommends_merge(self) -> None:
        """JW >= 0.90 with shared graph context → merge."""
        c = _make_probable_dup(jw=0.92, cj=0.30)
        rec = compute_structural_recommendation(c)
        assert rec.suggested_action == "merge"
        assert rec.confidence <= 0.90

    def test_high_similarity_no_context_recommends_merge(self) -> None:
        """JW >= 0.90 without context overlap → merge."""
        c = _make_probable_dup(jw=0.92, cj=0.0)
        rec = compute_structural_recommendation(c)
        assert rec.suggested_action == "merge"

    def test_canonical_direction_violation_mentions_two_paths(self) -> None:
        """canonical_direction_violation reasoning mentions both normalize and rename paths."""
        c = Candidate(
            run_id=_RUN_ID,
            schema_version=_SCHEMA_VERSION,
            candidate_type=CandidateType.canonical_violation,
            candidate_lane=CandidateLane.relationship,
            involved_element_refs=("rel_a",),
            severity=Severity.high,
            detection_method="canonical_direction_violation",
            collision_context={
                "rel_type": "MENTIONS",
                "actual_start_type": "Document",
                "actual_end_type": "Location",
            },
        )
        rec = compute_structural_recommendation(c)
        assert rec.suggested_action == "rename"
        assert rec.confidence == 0.65
        # Reasoning must mention both paths
        assert "normalize" in rec.reasoning.lower()
        assert "rename" in rec.reasoning.lower()
        assert "high_risk_override" in rec.reasoning

    def test_exact_node_duplicate_recommends_merge(self) -> None:
        """Exact node duplicate → merge at high confidence."""
        c = Candidate(
            run_id=_RUN_ID,
            schema_version=_SCHEMA_VERSION,
            candidate_type=CandidateType.exact_node_duplicate,
            candidate_lane=CandidateLane.node,
            involved_element_refs=("node_a", "node_b"),
            severity=Severity.critical,
            detection_method="exact_node_dedupe_key",
            collision_context={"duplicate_count": 2},
        )
        rec = compute_structural_recommendation(c)
        assert rec.suggested_action == "merge"
        assert rec.confidence == 0.95
