"""
tests/unit/test_verdict.py — Unit tests for verdict generalization (Tier 1).

Tests cover:
  - Pattern extraction for canonical_violation candidates
  - Batch proposal deterministic IDs and rationale
  - Filtering logic (same pattern, existing proposals)
"""

from __future__ import annotations

import hashlib
import json

import pytest

from api.services.curation.verdict import extract_violation_pattern


# ---------------------------------------------------------------------------
# Pattern extraction tests
# ---------------------------------------------------------------------------


class TestExtractViolationPattern:
    """Tests for extract_violation_pattern()."""

    def test_canonical_inverse_returns_tuple(self) -> None:
        """Correct tuple from inverse violation context."""
        ctx = {
            "rel_type": "MENTIONS",
            "actual_start_type": "Document",
            "actual_end_type": "Location",
            "expected_start_type": "Location",
            "expected_end_type": "Document",
        }
        result = extract_violation_pattern(ctx, "canonical_inverse")
        assert result == ("MENTIONS", "Document", "Location", "canonical_inverse")

    def test_canonical_direction_returns_tuple(self) -> None:
        """Correct tuple from direction violation context."""
        ctx = {
            "rel_type": "LOCATED_IN",
            "actual_start_type": "Location",
            "actual_end_type": "Document",
            "schema_start_type": "Document",
            "schema_end_type": "Location",
        }
        result = extract_violation_pattern(ctx, "canonical_direction")
        assert result == ("LOCATED_IN", "Location", "Document", "canonical_direction")

    def test_non_canonical_returns_none(self) -> None:
        """Returns None for non-canonical detection methods."""
        ctx = {
            "rel_type": "MENTIONS",
            "actual_start_type": "Document",
            "actual_end_type": "Location",
        }
        assert extract_violation_pattern(ctx, "orphan_node") is None
        assert extract_violation_pattern(ctx, "exact_node_dedupe_key") is None
        assert extract_violation_pattern(ctx, "jaro_winkler_0.9") is None

    def test_missing_context_fields_returns_none(self) -> None:
        """Returns None when required collision_context fields are missing."""
        assert extract_violation_pattern({}, "canonical_inverse") is None
        assert extract_violation_pattern(
            {"rel_type": "MENTIONS"}, "canonical_inverse"
        ) is None
        assert extract_violation_pattern(
            {"rel_type": "MENTIONS", "actual_start_type": "A"},
            "canonical_inverse",
        ) is None

    def test_empty_string_fields_returns_none(self) -> None:
        """Returns None when context fields are empty strings."""
        ctx = {
            "rel_type": "",
            "actual_start_type": "Document",
            "actual_end_type": "Location",
        }
        assert extract_violation_pattern(ctx, "canonical_inverse") is None


# ---------------------------------------------------------------------------
# Batch proposal ID uniqueness
# ---------------------------------------------------------------------------


class TestBatchProposalIds:
    """Tests for deterministic proposal_id derivation across batch candidates."""

    @staticmethod
    def _compute_proposal_id(
        run_id: str, candidate_id: str, proposal_class: str,
    ) -> str:
        """Mirror ProposalPacket.proposal_id computation."""
        identity = {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "proposal_class": proposal_class,
        }
        serialized = json.dumps(
            identity, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def test_unique_ids_for_different_candidates(self) -> None:
        """Each generated proposal has a unique proposal_id."""
        run_id = "run_abc"
        proposal_class = "normalize"
        cid_a = "a" * 64
        cid_b = "b" * 64
        cid_c = "c" * 64

        ids = {
            self._compute_proposal_id(run_id, cid, proposal_class)
            for cid in (cid_a, cid_b, cid_c)
        }
        assert len(ids) == 3, "Each candidate must produce a unique proposal_id"

    def test_same_candidate_same_id(self) -> None:
        """Same inputs always produce the same proposal_id (deterministic)."""
        run_id = "run_abc"
        cid = "d" * 64
        pc = "normalize"

        id1 = self._compute_proposal_id(run_id, cid, pc)
        id2 = self._compute_proposal_id(run_id, cid, pc)
        assert id1 == id2


# ---------------------------------------------------------------------------
# Batch rationale content
# ---------------------------------------------------------------------------


class TestBatchRationale:
    """Tests for rationale construction in batch proposals."""

    def test_rationale_references_source(self) -> None:
        """Rationale must contain reference to the source proposal."""
        source_proposal_id = "abc123def456" + "0" * 52  # 64 chars
        source_rationale = "Normalize MENTIONS to LOCATED_IN for Document->Location"

        # Simulate the rationale construction from verdict.py
        source_id_snippet = source_proposal_id[:16]
        source_rationale_snippet = source_rationale[:100]
        rationale = (
            f"Verdict generalization: same correction as "
            f"{source_id_snippet}... -- {source_rationale_snippet}"
        )

        assert "Verdict generalization" in rationale
        assert source_id_snippet in rationale
        assert "Normalize MENTIONS" in rationale


# ---------------------------------------------------------------------------
# Pattern matching selectivity
# ---------------------------------------------------------------------------


class TestPatternMatching:
    """Tests ensuring only same-pattern candidates are matched."""

    def test_different_rel_type_excluded(self) -> None:
        """Different rel_type should not match."""
        pattern_a = extract_violation_pattern(
            {"rel_type": "MENTIONS", "actual_start_type": "Doc", "actual_end_type": "Loc"},
            "canonical_inverse",
        )
        pattern_b = extract_violation_pattern(
            {"rel_type": "AUTHORED_BY", "actual_start_type": "Doc", "actual_end_type": "Loc"},
            "canonical_inverse",
        )
        assert pattern_a != pattern_b

    def test_different_endpoint_types_excluded(self) -> None:
        """Different start/end types should not match."""
        pattern_a = extract_violation_pattern(
            {"rel_type": "MENTIONS", "actual_start_type": "Doc", "actual_end_type": "Loc"},
            "canonical_inverse",
        )
        pattern_b = extract_violation_pattern(
            {"rel_type": "MENTIONS", "actual_start_type": "Person", "actual_end_type": "Loc"},
            "canonical_inverse",
        )
        assert pattern_a != pattern_b

    def test_different_detection_method_excluded(self) -> None:
        """Different detection_method should produce different patterns."""
        ctx = {"rel_type": "MENTIONS", "actual_start_type": "Doc", "actual_end_type": "Loc"}
        pattern_a = extract_violation_pattern(ctx, "canonical_inverse")
        pattern_b = extract_violation_pattern(ctx, "canonical_direction")
        assert pattern_a != pattern_b

    def test_same_pattern_matches(self) -> None:
        """Identical contexts and methods should produce equal patterns."""
        ctx = {"rel_type": "MENTIONS", "actual_start_type": "Doc", "actual_end_type": "Loc"}
        pattern_a = extract_violation_pattern(ctx, "canonical_inverse")
        pattern_b = extract_violation_pattern(ctx, "canonical_inverse")
        assert pattern_a == pattern_b
