"""
tests/unit/test_dedup_pipeline.py — Unit tests for pre-curation dedup pipeline.

No network, no LLM, no Neo4j.  Tests the pure-function stages of the pipeline:
  - _is_deterministic_canonical
  - _canonical_form
  - _property_overlap
  - _compute_composite_score
  - _check_contradictions
  - _classify_confidence
  - _validate_clusters (via DedupPipeline._enrich_candidates)
"""

from __future__ import annotations

import pytest

from api.graph.reader_models import GraphNodeRecord, NodeListResult, RelListResult
from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity
from api.services.curation.dedup_pipeline import (
    DedupPipeline,
    _canonical_form,
    _check_contradictions,
    _classify_confidence,
    _compute_composite_score,
    _is_deterministic_canonical,
    _property_overlap,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-dedup-pipeline"
_SCHEMA_VERSION = "test_schema_v1_hash_0000000000000000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candidate(
    val_a: str,
    val_b: str,
    key_a: str = "node_a_key",
    key_b: str = "node_b_key",
    jw: float = 0.92,
    tok: float = 0.80,
    cj: float = 0.50,
    detection_method: str = "jaro_winkler_0.9",
) -> Candidate:
    """Build a probable_duplicate candidate for testing."""
    return Candidate(
        run_id=_RUN_ID,
        schema_version=_SCHEMA_VERSION,
        candidate_type=CandidateType.probable_duplicate,
        candidate_lane=CandidateLane.node,
        involved_element_refs=(key_a, key_b),
        severity=Severity.high,
        detection_method=detection_method,
        collision_context={
            "value_a": val_a,
            "value_b": val_b,
            "jaro_winkler": jw,
            "token_overlap": tok,
            "context_jaccard": cj,
        },
    )


def _make_node(
    dedupe_key: str,
    node_type: str = "Person",
    **extra_props: str,
) -> GraphNodeRecord:
    """Build a GraphNodeRecord for testing."""
    props: dict = {"_primary_value": dedupe_key, **extra_props}
    return GraphNodeRecord(
        dedupe_key=dedupe_key,
        node_type=node_type,
        run_id=_RUN_ID,
        schema_version=_SCHEMA_VERSION,
        properties=props,
    )


# ===========================================================================
# Stage 1: _is_deterministic_canonical
# ===========================================================================


class TestIsDeterministicCanonical:
    """Tests for _is_deterministic_canonical()."""

    def test_case_only_difference(self) -> None:
        assert _is_deterministic_canonical("Engrid", "engrid") is True

    def test_case_and_whitespace(self) -> None:
        assert _is_deterministic_canonical("John Smith", "john  smith") is True

    def test_punctuation_difference(self) -> None:
        assert _is_deterministic_canonical("O'Brien", "OBrien") is True

    def test_identical_values_returns_false(self) -> None:
        assert _is_deterministic_canonical("Engrid", "Engrid") is False

    def test_substantive_difference_returns_false(self) -> None:
        assert _is_deterministic_canonical("Engrid", "Ingrid") is False

    def test_empty_strings(self) -> None:
        assert _is_deterministic_canonical("", "") is False

    def test_hyphenated_vs_space(self) -> None:
        assert _is_deterministic_canonical("Mary-Jane", "Mary Jane") is True

    def test_accented_vs_plain(self) -> None:
        # Accented characters are stripped by the regex, so "café" → "caf"
        # vs "cafe" → "cafe" — these are different
        assert _is_deterministic_canonical("café", "cafe") is False


# ===========================================================================
# _canonical_form
# ===========================================================================


class TestCanonicalForm:
    """Tests for _canonical_form()."""

    def test_longer_value_title_cased(self) -> None:
        assert _canonical_form("engrid", "ENGRID BOSS") == "Engrid Boss"

    def test_equal_length_first_value(self) -> None:
        assert _canonical_form("john", "JOHN") == "John"

    def test_preserves_title_case(self) -> None:
        assert _canonical_form("alice smith", "Alice Smith") == "Alice Smith"


# ===========================================================================
# Stage 3: _property_overlap
# ===========================================================================


class TestPropertyOverlap:
    """Tests for _property_overlap()."""

    def test_identical_properties(self) -> None:
        props = {"age": "30", "city": "NYC"}
        assert _property_overlap(props, props) == 1.0

    def test_no_shared_keys(self) -> None:
        assert _property_overlap({"a": "1"}, {"b": "2"}) == 0.0

    def test_partial_overlap(self) -> None:
        a = {"age": "30", "city": "NYC"}
        b = {"age": "30", "city": "LA"}
        # 2 total keys, 1 matching (age) → 0.5
        assert _property_overlap(a, b) == 0.5

    def test_governance_keys_excluded(self) -> None:
        a = {"_dedupe_key": "x", "age": "30"}
        b = {"_dedupe_key": "y", "age": "30"}
        # Only "age" is compared → 1.0
        assert _property_overlap(a, b) == 1.0

    def test_empty_dicts(self) -> None:
        assert _property_overlap({}, {}) == 0.0


# ===========================================================================
# Stage 3: _compute_composite_score
# ===========================================================================


class TestCompositeScore:
    """Tests for _compute_composite_score()."""

    def test_perfect_scores(self) -> None:
        ctx = {"jaro_winkler": 1.0, "token_overlap": 1.0, "context_jaccard": 1.0}
        props = {"age": "30"}
        score = _compute_composite_score(ctx, props, props)
        # 0.35*1 + 0.25*1 + 0.25*1 + 0.15*1 = 1.0
        assert abs(score - 1.0) < 0.001

    def test_zero_scores(self) -> None:
        ctx = {"jaro_winkler": 0.0, "token_overlap": 0.0, "context_jaccard": 0.0}
        score = _compute_composite_score(ctx, {}, {})
        assert score == 0.0

    def test_mixed_scores(self) -> None:
        ctx = {"jaro_winkler": 0.95, "token_overlap": 0.80, "context_jaccard": 0.60}
        props_a = {"age": "30"}
        props_b = {"age": "30"}
        score = _compute_composite_score(ctx, props_a, props_b)
        expected = 0.35 * 0.95 + 0.25 * 0.80 + 0.25 * 0.60 + 0.15 * 1.0
        assert abs(score - expected) < 0.001

    def test_missing_context_fields(self) -> None:
        ctx = {}
        score = _compute_composite_score(ctx, {}, {})
        assert score == 0.0


# ===========================================================================
# Stage 4: _check_contradictions
# ===========================================================================


class TestCheckContradictions:
    """Tests for _check_contradictions()."""

    def test_no_contradictions(self) -> None:
        a = {"age": "30", "city": "NYC"}
        b = {"age": "30", "city": "NYC"}
        assert _check_contradictions(a, b) == []

    def test_with_contradiction(self) -> None:
        a = {"age": "30", "city": "NYC"}
        b = {"age": "30", "city": "LA"}
        assert _check_contradictions(a, b) == ["city"]

    def test_governance_keys_excluded(self) -> None:
        a = {"_dedupe_key": "x", "age": "30"}
        b = {"_dedupe_key": "y", "age": "30"}
        assert _check_contradictions(a, b) == []

    def test_empty_values_not_contradictions(self) -> None:
        a = {"city": ""}
        b = {"city": "NYC"}
        assert _check_contradictions(a, b) == []

    def test_none_values_not_contradictions(self) -> None:
        a = {"city": None}
        b = {"city": "NYC"}
        assert _check_contradictions(a, b) == []

    def test_multiple_contradictions_sorted(self) -> None:
        a = {"city": "NYC", "age": "30"}
        b = {"city": "LA", "age": "25"}
        assert _check_contradictions(a, b) == ["age", "city"]


# ===========================================================================
# Stage 5: _classify_confidence
# ===========================================================================


class TestClassifyConfidence:
    """Tests for _classify_confidence()."""

    def test_high_no_contradictions(self) -> None:
        assert _classify_confidence(0.85, False) == "high"

    def test_high_with_contradictions_becomes_medium(self) -> None:
        assert _classify_confidence(0.85, True) == "medium"

    def test_medium_range(self) -> None:
        assert _classify_confidence(0.65, False) == "medium"

    def test_low_score(self) -> None:
        assert _classify_confidence(0.50, False) == "low"

    def test_exact_threshold_high(self) -> None:
        assert _classify_confidence(0.80, False) == "high"

    def test_exact_threshold_medium(self) -> None:
        assert _classify_confidence(0.60, False) == "medium"

    def test_below_medium_threshold(self) -> None:
        assert _classify_confidence(0.59, False) == "low"

    def test_low_composite_high_jw_becomes_medium(self) -> None:
        """jw >= 0.90 prevents deferral even with low composite score."""
        assert _classify_confidence(0.49, False, jaro_winkler=0.92) == "medium"

    def test_low_composite_low_jw_stays_low(self) -> None:
        """jw below 0.90 does not rescue a low composite score."""
        assert _classify_confidence(0.49, False, jaro_winkler=0.85) == "low"

    def test_high_jw_does_not_override_high_band(self) -> None:
        """High composite score still yields high regardless of jw."""
        assert _classify_confidence(0.85, False, jaro_winkler=0.95) == "high"


# ===========================================================================
# Stage 6: Cluster validation (via _enrich_candidates)
# ===========================================================================


class TestClusterValidation:
    """Tests for cluster validation logic in _enrich_candidates."""

    def test_coherent_cluster_stays_high(self) -> None:
        """Same node type, no contradictions, shared props, under size cap → high."""
        c1 = _make_candidate("Alice", "alice", "k1", "k2", jw=0.98, tok=0.95, cj=0.90)
        c2 = _make_candidate("Alice", "alice", "k2", "k3", jw=0.98, tok=0.95, cj=0.90)
        # Add matching non-governance properties so property_overlap > 0
        nodes = {
            "k1": _make_node("k1", city="NYC", role="analyst"),
            "k2": _make_node("k2", city="NYC", role="analyst"),
            "k3": _make_node("k3", city="NYC", role="analyst"),
        }

        pipeline = DedupPipeline.__new__(DedupPipeline)
        pipeline._run_id = _RUN_ID
        pipeline._schema = None
        enriched = pipeline._enrich_candidates([c1, c2], nodes)

        # Score: 0.35*0.98 + 0.25*0.95 + 0.25*0.90 + 0.15*1.0 = 0.955
        for c in enriched:
            band = c.collision_context.get("confidence_band")
            assert band == "high", f"Expected high, got {band}"

    def test_mixed_node_types_downgraded(self) -> None:
        """Cluster with different node types → downgraded to medium."""
        # Use high scores + shared properties to ensure initial band would be "high"
        c = _make_candidate("ACME", "acme", "k1", "k2", jw=0.98, tok=0.95, cj=0.90)
        nodes = {
            "k1": _make_node("k1", node_type="Person", city="NYC"),
            "k2": _make_node("k2", node_type="Organization", city="NYC"),
        }

        pipeline = DedupPipeline.__new__(DedupPipeline)
        pipeline._run_id = _RUN_ID
        enriched = pipeline._enrich_candidates([c], nodes)

        assert enriched[0].collision_context["confidence_band"] == "medium"
        assert enriched[0].collision_context.get("cluster_downgraded") is True

    def test_cluster_with_contradictions_downgraded(self) -> None:
        """Cluster where a pair has property conflicts → downgraded."""
        c = _make_candidate("Alice", "alice", "k1", "k2", jw=0.95, tok=0.90, cj=0.80)
        nodes = {
            "k1": _make_node("k1", age="30"),
            "k2": _make_node("k2", age="25"),  # contradiction
        }

        pipeline = DedupPipeline.__new__(DedupPipeline)
        pipeline._run_id = _RUN_ID
        enriched = pipeline._enrich_candidates([c], nodes)

        # Has contradictions + high score → medium
        assert enriched[0].collision_context["confidence_band"] == "medium"


# ===========================================================================
# Integration of pure stages
# ===========================================================================


class TestEnrichCandidates:
    """End-to-end test of stages 3-6 through _enrich_candidates."""

    def test_low_similarity_classified_low(self) -> None:
        c = _make_candidate("Alice", "Bob", "k1", "k2", jw=0.40, tok=0.20, cj=0.10)
        nodes = {"k1": _make_node("k1"), "k2": _make_node("k2")}

        pipeline = DedupPipeline.__new__(DedupPipeline)
        pipeline._run_id = _RUN_ID
        enriched = pipeline._enrich_candidates([c], nodes)

        assert enriched[0].collision_context["confidence_band"] == "low"
        assert enriched[0].collision_context["composite_score"] < 0.60

    def test_medium_similarity_classified_medium(self) -> None:
        # Need score in [0.60, 0.80) range for medium
        # 0.35*0.85 + 0.25*0.75 + 0.25*0.65 + 0.15*0 = 0.2975 + 0.1875 + 0.1625 = 0.6475
        c = _make_candidate("Alice S", "Alice Smith", "k1", "k2", jw=0.85, tok=0.75, cj=0.65)
        nodes = {"k1": _make_node("k1"), "k2": _make_node("k2")}

        pipeline = DedupPipeline.__new__(DedupPipeline)
        pipeline._run_id = _RUN_ID
        enriched = pipeline._enrich_candidates([c], nodes)

        band = enriched[0].collision_context["confidence_band"]
        score = enriched[0].collision_context["composite_score"]
        assert band == "medium", f"score={score}, band={band}"
