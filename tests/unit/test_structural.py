"""
tests/unit/test_structural.py — Structural recommendation tests.

Tests the deterministic structural recommendation logic for probable_duplicate,
canonical_violation, and other candidate types.  Also tests the
_validate_normalize_target guard that prevents no-op normalize proposals.

No network, no LLM, no Neo4j.
"""

from __future__ import annotations

from api.agents.models import AgentProposalOutput
from api.agents.proposal import _validate_normalize_target
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

    def test_high_similarity_no_context_low_tok_defers(self) -> None:
        """JW >= 0.90 without context overlap and low tok → defer."""
        c = _make_probable_dup(jw=0.92, cj=0.0, tok=0.60)
        rec = compute_structural_recommendation(c)
        assert rec.suggested_action == "defer"
        assert rec.confidence == 0.45

    def test_high_similarity_no_context_high_tok_merges(self) -> None:
        """JW >= 0.90 without context overlap but high tok → merge."""
        c = _make_probable_dup(jw=0.92, cj=0.0, tok=0.85)
        rec = compute_structural_recommendation(c)
        assert rec.suggested_action == "merge"
        assert rec.confidence == 0.65

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
        assert rec.suggested_action == "normalize"
        assert rec.confidence == 0.50
        # Reasoning must mention both paths
        assert "normalize" in rec.reasoning.lower()
        assert "rename" in rec.reasoning.lower()
        assert "high_risk_override" in rec.reasoning

    def test_containment_no_corroboration_defers(self) -> None:
        """Token containment + CJ=0 → defer."""
        c = _make_probable_dup(val_a="ingrid bos", val_b="ingrid bos van dijk", jw=0.88, tok=0.667, cj=0.0)
        c.collision_context["containment"] = True
        rec = compute_structural_recommendation(c)
        assert rec.suggested_action == "defer"
        assert rec.confidence == 0.55

    def test_containment_with_corroboration_merges(self) -> None:
        """Token containment + CJ>0 → merge."""
        c = _make_probable_dup(val_a="ingrid bos", val_b="ingrid bos van dijk", jw=0.88, tok=0.667, cj=0.30)
        c.collision_context["containment"] = True
        rec = compute_structural_recommendation(c)
        assert rec.suggested_action == "merge"
        assert rec.confidence == 0.80

    def test_name_subset_no_corroboration_defers(self) -> None:
        """Name subset (JW>=0.90) + CJ=0 → defer."""
        c = _make_probable_dup(val_a="ingrid", val_b="ingrid bos", jw=0.92, tok=0.667, cj=0.0)
        rec = compute_structural_recommendation(c)
        # Pattern 3 (name subset, JW>=0.90, CJ=0) → defer
        assert rec.suggested_action == "defer"
        assert rec.confidence == 0.50

    def test_jw_092_tok_04_cj_0_defers(self) -> None:
        """JW=0.92 + tok=0.4 + CJ=0 → defer (insufficient corroboration)."""
        c = _make_probable_dup(val_a="Zenith Corp", val_b="Zenith Co", jw=0.92, tok=0.40, cj=0.0)
        rec = compute_structural_recommendation(c)
        assert rec.suggested_action == "defer"
        assert rec.confidence == 0.45

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


# ===========================================================================
# Normalize target validation tests
# ===========================================================================

_CANDIDATE_ID_HEX = "a" * 64


def _make_canonical_candidate(
    detection_method: str = "canonical_direction_violation",
    rel_type: str = "MENTIONED_IN",
    actual_start: str = "Organization",
    actual_end: str = "Document",
) -> Candidate:
    """Helper: canonical violation candidate for normalize target tests."""
    ctx: dict = {
        "rel_type": rel_type,
        "actual_start_type": actual_start,
        "actual_end_type": actual_end,
        "rel_dedupe_key": f"{rel_type}::start_dk::end_dk::sv",
        "start_dedupe_key": "start_dk",
        "end_dedupe_key": "end_dk",
    }
    if detection_method == "canonical_inverse_violation":
        ctx["expected_start_type"] = actual_end
        ctx["expected_end_type"] = actual_start
    return Candidate(
        run_id=_RUN_ID,
        schema_version=_SCHEMA_VERSION,
        candidate_type=CandidateType.canonical_violation,
        candidate_lane=CandidateLane.relationship,
        involved_element_refs=(
            f"{rel_type}::start_dk::end_dk::sv",
            "start_dk",
            "end_dk",
        ),
        severity=Severity.high,
        detection_method=detection_method,
        collision_context=ctx,
    )


def _make_normalize_output(
    normalize_target: str = "",
) -> AgentProposalOutput:
    """Helper: LLM output proposing normalize."""
    return AgentProposalOutput(
        candidate_id=_CANDIDATE_ID_HEX,
        proposal_class="normalize",
        rule_ids=(),
        evidence_ids=(),
        rationale="Test normalize rationale.",
        confidence_score=0.80,
        normalize_target=normalize_target,
    )


class TestValidateNormalizeTarget:
    """Tests for _validate_normalize_target guard."""

    def test_direction_violation_no_target_deferred(self) -> None:
        """Direction violation + empty normalize_target → defer."""
        candidate = _make_canonical_candidate("canonical_direction_violation")
        output = _make_normalize_output(normalize_target="")

        result = _validate_normalize_target(output, candidate)

        assert result.proposal_class == "defer"
        assert result.confidence_score <= 0.30
        assert "Deferred" in result.rationale

    def test_direction_violation_with_target_unchanged(self) -> None:
        """Direction violation + valid normalize_target → unchanged."""
        candidate = _make_canonical_candidate("canonical_direction_violation")
        output = _make_normalize_output(normalize_target="REFERENCED_IN")

        result = _validate_normalize_target(output, candidate)

        assert result.proposal_class == "normalize"
        assert result.normalize_target == "REFERENCED_IN"

    def test_inverse_violation_no_target_auto_filled(self) -> None:
        """Inverse violation + empty normalize_target → auto-fill with rel_type."""
        candidate = _make_canonical_candidate("canonical_inverse_violation")
        output = _make_normalize_output(normalize_target="")

        result = _validate_normalize_target(output, candidate)

        assert result.proposal_class == "normalize"
        assert result.normalize_target == "MENTIONED_IN"

    def test_non_normalize_proposal_unchanged(self) -> None:
        """Non-normalize proposals pass through unchanged."""
        candidate = _make_canonical_candidate("canonical_direction_violation")
        output = AgentProposalOutput(
            candidate_id=_CANDIDATE_ID_HEX,
            proposal_class="delete",
            rule_ids=(),
            evidence_ids=(),
            rationale="Delete it.",
            confidence_score=0.90,
        )

        result = _validate_normalize_target(output, candidate)

        assert result.proposal_class == "delete"

    def test_non_canonical_candidate_unchanged(self) -> None:
        """Normalize proposals for non-canonical candidates pass through."""
        candidate = Candidate(
            run_id=_RUN_ID,
            schema_version=_SCHEMA_VERSION,
            candidate_type=CandidateType.structural_anomaly,
            candidate_lane=CandidateLane.structural,
            involved_element_refs=("node_a",),
            severity=Severity.medium,
            detection_method="missing_provenance_node",
            collision_context={"dedupe_key": "node_a"},
        )
        output = _make_normalize_output(normalize_target="")

        result = _validate_normalize_target(output, candidate)

        assert result.proposal_class == "normalize"
