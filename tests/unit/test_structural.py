"""
tests/unit/test_structural.py — Structural recommendation escalation tests.

Tests the escalation logic: when a prior canonicalize proposal was applied for a
candidate that reappears, the structural recommendation escalates to merge.

No network, no LLM, no Neo4j.
"""

from __future__ import annotations

import pytest

from api.agents.models import PriorProposalSummary, StructuralRecommendation
from api.agents.structural import compute_structural_recommendation
from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-structural-escalation"
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
# Escalation tests
# ===========================================================================


class TestStructuralEscalation:
    """Tests for prior proposal escalation in compute_structural_recommendation."""

    def test_no_prior_proposals_uses_normal_logic(self) -> None:
        """Without prior proposals, normal recommendation logic applies."""
        c = _make_probable_dup(jw=0.92, cj=0.30)
        rec = compute_structural_recommendation(c, prior_proposals=None)
        # JW >= 0.90 + CJ > 0 → merge (normal logic)
        assert rec.suggested_action == "merge"
        assert rec.confidence <= 0.90  # Normal merge confidence

    def test_prior_canonicalize_executed_escalates_to_merge(self) -> None:
        """A prior executed canonicalize should escalate to merge (higher confidence)."""
        c = _make_probable_dup(jw=0.92, cj=0.0)
        prior = [
            PriorProposalSummary(
                proposal_class="canonicalize",
                confidence=0.85,
                outcome="executed",
            )
        ]
        rec = compute_structural_recommendation(c, prior_proposals=prior)
        assert rec.suggested_action == "merge"
        assert rec.confidence == 0.80  # Escalation confidence
        assert "escalat" in rec.reasoning.lower()

    def test_prior_canonicalize_pending_no_escalation(self) -> None:
        """A pending canonicalize should NOT trigger escalation (still merge from heuristic)."""
        c = _make_probable_dup(jw=0.92, cj=0.0)
        prior = [
            PriorProposalSummary(
                proposal_class="canonicalize",
                confidence=0.85,
                outcome="pending",
            )
        ]
        rec = compute_structural_recommendation(c, prior_proposals=prior)
        # jw >= 0.90 → merge directly (no escalation needed)
        assert rec.suggested_action == "merge"

    def test_prior_merge_executed_no_escalation(self) -> None:
        """A prior merge (not canonicalize) should not trigger escalation."""
        c = _make_probable_dup(jw=0.92, cj=0.0)
        prior = [
            PriorProposalSummary(
                proposal_class="merge",
                confidence=0.90,
                outcome="executed",
            )
        ]
        rec = compute_structural_recommendation(c, prior_proposals=prior)
        # jw >= 0.90 → merge directly
        assert rec.suggested_action == "merge"

    def test_empty_prior_list_still_merges(self) -> None:
        """Empty prior list — jw >= 0.90 produces merge directly."""
        c = _make_probable_dup(jw=0.92, cj=0.0)
        rec = compute_structural_recommendation(c, prior_proposals=[])
        assert rec.suggested_action == "merge"

    def test_multiple_prior_proposals_first_match_wins(self) -> None:
        """Multiple priors — first matching canonicalize+executed triggers escalation."""
        c = _make_probable_dup(jw=0.92, cj=0.0)
        prior = [
            PriorProposalSummary(
                proposal_class="canonicalize",
                confidence=0.70,
                outcome="rejected",
            ),
            PriorProposalSummary(
                proposal_class="canonicalize",
                confidence=0.85,
                outcome="executed",
            ),
        ]
        rec = compute_structural_recommendation(c, prior_proposals=prior)
        assert rec.suggested_action == "merge"
        assert rec.confidence == 0.80  # Escalation confidence

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

    def test_non_probable_duplicate_ignores_prior_proposals(self) -> None:
        """Prior proposals should only affect probable_duplicate candidates."""
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
        prior = [
            PriorProposalSummary(
                proposal_class="canonicalize",
                confidence=0.85,
                outcome="executed",
            )
        ]
        rec = compute_structural_recommendation(c, prior_proposals=prior)
        # Exact node dup → merge at 0.95 (not escalated)
        assert rec.suggested_action == "merge"
        assert rec.confidence == 0.95
