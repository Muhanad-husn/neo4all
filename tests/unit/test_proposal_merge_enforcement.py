"""
tests/unit/test_proposal_merge_enforcement.py — Unit tests for _enforce_merge_for_duplicates.

No network, no LLM, no Neo4j.

Coverage:
  - Override case: probable_duplicate + structural merge + LLM canonicalize → enforced merge.
  - Override case: exact_node_duplicate + structural merge + LLM canonicalize → enforced merge.
  - Pass-through: structural merge + LLM merge → no change.
  - Pass-through: structural canonicalize + LLM canonicalize → no change (structural didn't say merge).
  - Pass-through: canonical_violation candidate → no change (wrong candidate type).
  - Pass-through: no structural recommendation → no change.
  - Confidence: enforced confidence is max(structural, llm).
"""

from __future__ import annotations

from api.agents.models import (
    AgentProposalOutput,
    EvidenceReport,
    StructuralRecommendation,
)
from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity


def _make_candidate(
    candidate_type: CandidateType = CandidateType.probable_duplicate,
) -> Candidate:
    """Build a minimal Candidate for testing."""
    return Candidate(
        run_id="run-1",
        schema_version="sv-1",
        candidate_type=candidate_type,
        candidate_lane=CandidateLane.node,
        involved_element_refs=("node-a", "node-b"),
        severity=Severity.high,
        detection_method="jaro_winkler_name",
        collision_context={"jw_score": 0.92},
    )


def _make_output(
    candidate_id: str,
    proposal_class: str = "canonicalize",
    confidence: float = 0.85,
) -> AgentProposalOutput:
    return AgentProposalOutput(
        candidate_id=candidate_id,
        proposal_class=proposal_class,
        rule_ids=(),
        evidence_ids=(),
        rationale="Test rationale",
        confidence_score=confidence,
    )


def _make_evidence(
    action: str = "merge",
    confidence: float = 0.90,
) -> EvidenceReport:
    return EvidenceReport(
        items=(),
        sufficiency_score=0.8,
        sufficient=True,
        structural_recommendation=StructuralRecommendation(
            suggested_action=action,
            confidence=confidence,
            reasoning="High JW similarity",
        ),
        run_id="run-1",
    )


class TestEnforceMergeForDuplicates:
    """Tests for _enforce_merge_for_duplicates."""

    def test_override_probable_duplicate(self) -> None:
        """probable_duplicate + structural merge + LLM canonicalize → merge."""
        from api.agents.proposal import _enforce_merge_for_duplicates

        candidate = _make_candidate(CandidateType.probable_duplicate)
        output = _make_output(candidate.candidate_id, "canonicalize", 0.85)
        evidence = _make_evidence("merge", 0.90)

        result = _enforce_merge_for_duplicates(output, candidate, evidence)

        assert result.proposal_class == "merge"
        assert result.confidence_score == 0.90  # max(0.90, 0.85)
        assert result.rationale == output.rationale
        assert result.candidate_id == output.candidate_id

    def test_override_exact_node_duplicate(self) -> None:
        """exact_node_duplicate + structural merge + LLM canonicalize → merge."""
        from api.agents.proposal import _enforce_merge_for_duplicates

        candidate = _make_candidate(CandidateType.exact_node_duplicate)
        output = _make_output(candidate.candidate_id, "canonicalize", 0.80)
        evidence = _make_evidence("merge", 0.75)

        result = _enforce_merge_for_duplicates(output, candidate, evidence)

        assert result.proposal_class == "merge"
        assert result.confidence_score == 0.80  # max(0.75, 0.80)

    def test_passthrough_llm_already_merge(self) -> None:
        """LLM already returns merge → no override."""
        from api.agents.proposal import _enforce_merge_for_duplicates

        candidate = _make_candidate()
        output = _make_output(candidate.candidate_id, "merge", 0.85)
        evidence = _make_evidence("merge", 0.90)

        result = _enforce_merge_for_duplicates(output, candidate, evidence)

        assert result is output  # unchanged, same object

    def test_passthrough_structural_not_merge(self) -> None:
        """Structural recommends canonicalize, not merge → no override."""
        from api.agents.proposal import _enforce_merge_for_duplicates

        candidate = _make_candidate()
        output = _make_output(candidate.candidate_id, "canonicalize", 0.85)
        evidence = _make_evidence("canonicalize", 0.70)

        result = _enforce_merge_for_duplicates(output, candidate, evidence)

        assert result is output

    def test_passthrough_wrong_candidate_type(self) -> None:
        """canonical_violation candidate → no override regardless of classes."""
        from api.agents.proposal import _enforce_merge_for_duplicates

        candidate = _make_candidate(CandidateType.canonical_violation)
        output = _make_output(candidate.candidate_id, "canonicalize", 0.85)
        evidence = _make_evidence("merge", 0.90)

        result = _enforce_merge_for_duplicates(output, candidate, evidence)

        assert result is output

    def test_passthrough_no_structural_recommendation(self) -> None:
        """No structural recommendation → no override."""
        from api.agents.proposal import _enforce_merge_for_duplicates

        candidate = _make_candidate()
        output = _make_output(candidate.candidate_id, "canonicalize", 0.85)
        evidence = EvidenceReport(
            items=(),
            sufficiency_score=0.8,
            sufficient=True,
            structural_recommendation=None,
            run_id="run-1",
        )

        result = _enforce_merge_for_duplicates(output, candidate, evidence)

        assert result is output

    def test_confidence_takes_higher(self) -> None:
        """Enforced confidence is max of structural and LLM."""
        from api.agents.proposal import _enforce_merge_for_duplicates

        candidate = _make_candidate()
        output = _make_output(candidate.candidate_id, "canonicalize", 0.95)
        evidence = _make_evidence("merge", 0.80)

        result = _enforce_merge_for_duplicates(output, candidate, evidence)

        assert result.proposal_class == "merge"
        assert result.confidence_score == 0.95  # LLM was higher
