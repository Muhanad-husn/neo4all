"""
api/agents/structural.py — Deterministic structural recommendation.

Pure function that maps (candidate_type, detection_method, collision_context)
to a StructuralRecommendation.  No LLM, no I/O, no randomness.

This is the primary decision input for Agent-P when textual evidence is
sparse or absent.  The recommendation is computed from governed, deterministic
detector outputs — the same metrics that generated the candidate in the first
place.
"""

from __future__ import annotations

from api.agents.models import StructuralRecommendation
from api.models.candidate import Candidate


def compute_structural_recommendation(
    candidate: Candidate,
) -> StructuralRecommendation:
    """Derive a deterministic curation recommendation from collision_context.

    Maps each (candidate_type, detection_method) pair to a suggested action
    and confidence score based on the structural metrics.  Returns a
    StructuralRecommendation that Agent-P can adopt or override.

    This function is intentionally exhaustive — every known detector path
    produces a concrete recommendation.  Unknown paths fall back to defer.
    """
    ctx = candidate.collision_context
    method = candidate.detection_method
    ctype = str(candidate.candidate_type)

    # ----- exact_node_duplicate -------------------------------------------
    if ctype == "exact_node_duplicate":
        count = ctx.get("duplicate_count", 2)
        return StructuralRecommendation(
            suggested_action="merge",
            confidence=0.95,
            reasoning=(
                f"Exact node duplicate: {count} nodes share the same "
                f"dedupe key. These are identical entries that should be "
                f"merged into one."
            ),
        )

    # ----- exact_rel_duplicate --------------------------------------------
    if ctype == "exact_rel_duplicate":
        count = ctx.get("duplicate_count", 2)
        return StructuralRecommendation(
            suggested_action="merge",
            confidence=0.95,
            reasoning=(
                f"Exact relationship duplicate: {count} relationships share "
                f"the same type, endpoints, and dedupe key. Merge into one."
            ),
        )

    # ----- probable_duplicate ---------------------------------------------
    if ctype == "probable_duplicate":
        return _recommend_probable_duplicate(ctx)

    # ----- canonical_violation --------------------------------------------
    if ctype == "canonical_violation":
        return _recommend_canonical_violation(ctx, method)

    # ----- structural_anomaly ---------------------------------------------
    if ctype == "structural_anomaly":
        return _recommend_structural_anomaly(ctx, method)

    # ----- fallback -------------------------------------------------------
    return StructuralRecommendation(
        suggested_action="defer",
        confidence=0.20,
        reasoning=(
            f"Candidate type '{ctype}' with detection method '{method}' "
            f"does not map to a known structural recommendation."
        ),
    )


# ---------------------------------------------------------------------------
# Probable duplicate sub-logic
# ---------------------------------------------------------------------------


def _recommend_probable_duplicate(
    ctx: dict,
) -> StructuralRecommendation:
    """Recommendation for probable_duplicate candidates."""
    jw: float = ctx.get("jaro_winkler", 0.0)
    tok: float = ctx.get("token_overlap", 0.0)
    cj: float = ctx.get("context_jaccard", 0.0)
    val_a: str = str(ctx.get("value_a", ""))
    val_b: str = str(ctx.get("value_b", ""))

    # Pattern 1: Case-only difference
    if val_a and val_b and val_a.lower() == val_b.lower():
        return StructuralRecommendation(
            suggested_action="canonicalize",
            confidence=min(0.95, max(jw, 0.90)),
            reasoning=(
                f"Values '{val_a}' and '{val_b}' differ only in letter "
                f"casing (similarity: {jw:.2f}). Standardise to canonical form."
            ),
        )

    # Pattern 5: Near-identical (typo / formatting)
    if jw >= 0.95:
        conf = round(0.90 + min(cj, 0.05), 2)
        return StructuralRecommendation(
            suggested_action="canonicalize",
            confidence=min(1.0, conf),
            reasoning=(
                f"Near-identical values '{val_a}' and '{val_b}' "
                f"(similarity: {jw:.2f}). Minor formatting or "
                f"typographical difference."
            ),
        )

    # Pattern 6: High similarity + shared graph neighbourhood → merge
    if jw >= 0.90 and cj > 0:
        conf = round(0.75 + min(cj, 0.15), 2)
        return StructuralRecommendation(
            suggested_action="merge",
            confidence=conf,
            reasoning=(
                f"High similarity between '{val_a}' and '{val_b}' "
                f"(similarity: {jw:.2f}) with shared graph neighbours "
                f"(context overlap: {cj:.2f}). Strong evidence these "
                f"represent the same entity."
            ),
        )

    # High similarity, no shared context → canonicalize (safer)
    if jw >= 0.90:
        conf = round(0.70 + min(tok * 0.1, 0.10), 2)
        return StructuralRecommendation(
            suggested_action="canonicalize",
            confidence=conf,
            reasoning=(
                f"High similarity between '{val_a}' and '{val_b}' "
                f"(similarity: {jw:.2f}, word overlap: {tok:.2f}). "
                f"No shared graph neighbours — canonicalize rather "
                f"than merge."
            ),
        )

    # Below thresholds (shouldn't happen — detector gate is 0.90)
    return StructuralRecommendation(
        suggested_action="defer",
        confidence=0.30,
        reasoning=(
            f"Similarity between '{val_a}' and '{val_b}' is below "
            f"threshold (similarity: {jw:.2f}). Insufficient structural "
            f"evidence for action."
        ),
    )


# ---------------------------------------------------------------------------
# Canonical violation sub-logic
# ---------------------------------------------------------------------------


def _recommend_canonical_violation(
    ctx: dict,
    method: str,
) -> StructuralRecommendation:
    """Recommendation for canonical_violation candidates."""
    rel_type = ctx.get("rel_type", "unknown")

    if method == "canonical_inverse_violation":
        return StructuralRecommendation(
            suggested_action="normalize",
            confidence=0.90,
            reasoning=(
                f"Relationship '{rel_type}' has reversed direction: "
                f"connects {ctx.get('actual_start_type', '?')} -> "
                f"{ctx.get('actual_end_type', '?')} but schema expects "
                f"{ctx.get('expected_start_type', '?')} -> "
                f"{ctx.get('expected_end_type', '?')}. Reverse to match "
                f"schema."
            ),
        )

    # canonical_direction_violation — node types don't match any schema edge.
    # The relationship may be real but mislabeled (e.g., HAS_CONTRACT between
    # Org→Org should be WORKS_FOR).  Suggest rename rather than delete so the
    # LLM can evaluate whether the connection is valid under a different type.
    return StructuralRecommendation(
        suggested_action="rename",
        confidence=0.65,
        reasoning=(
            f"Relationship '{rel_type}' connects "
            f"{ctx.get('actual_start_type', '?')} -> "
            f"{ctx.get('actual_end_type', '?')} which matches no valid "
            f"schema direction for this type. The connection may be real "
            f"but mislabeled — consider whether a different relationship "
            f"type fits this pair of node types, or delete if the "
            f"relationship is truly invalid."
        ),
    )


# ---------------------------------------------------------------------------
# Structural anomaly sub-logic
# ---------------------------------------------------------------------------


def _recommend_structural_anomaly(
    ctx: dict,
    method: str,
) -> StructuralRecommendation:
    """Recommendation for structural_anomaly candidates."""
    dedupe_key = ctx.get("dedupe_key", "unknown")

    if method == "orphan_node":
        return StructuralRecommendation(
            suggested_action="delete",
            confidence=0.70,
            reasoning=(
                f"Node '{dedupe_key}' has no relationships (orphan). "
                f"Typically indicates an extraction error or incomplete data."
            ),
        )

    if method == "degree_outlier":
        total = ctx.get("total_degree", "?")
        mean = ctx.get("mean_degree", 0)
        threshold = ctx.get("threshold", 0)
        mean_str = f"{mean:.1f}" if isinstance(mean, (int, float)) else str(mean)
        threshold_str = f"{threshold:.1f}" if isinstance(threshold, (int, float)) else str(threshold)
        return StructuralRecommendation(
            suggested_action="defer",
            confidence=0.30,
            reasoning=(
                f"Node '{dedupe_key}' has {total} connections "
                f"(mean: {mean_str}, threshold: {threshold_str}). "
                f"Degree outlier requires human review."
            ),
        )

    if method == "missing_provenance_node":
        return StructuralRecommendation(
            suggested_action="normalize",
            confidence=0.90,
            reasoning=(
                f"Node '{dedupe_key}' (type: {ctx.get('node_type', '?')}) "
                f"is missing required provenance field "
                f"'{ctx.get('missing_field', '?')}'. Add the missing "
                f"metadata."
            ),
        )

    if method == "missing_provenance_rel":
        return StructuralRecommendation(
            suggested_action="normalize",
            confidence=0.90,
            reasoning=(
                f"Relationship '{dedupe_key}' "
                f"(type: {ctx.get('rel_type', '?')}) is missing required "
                f"provenance field '{ctx.get('missing_field', '?')}'. "
                f"Add the missing metadata."
            ),
        )

    if method == "qualifier_missing":
        return StructuralRecommendation(
            suggested_action="normalize",
            confidence=0.80,
            reasoning=(
                f"Node '{dedupe_key}' (type: {ctx.get('node_type', '?')}) "
                f"is missing qualifier property "
                f"'{ctx.get('qualifier_property', '?')}'. Add the missing "
                f"property."
            ),
        )

    # Unknown structural anomaly sub-type
    return StructuralRecommendation(
        suggested_action="defer",
        confidence=0.25,
        reasoning=(
            f"Structural anomaly '{method}' on '{dedupe_key}' does not "
            f"map to a known recommendation pattern."
        ),
    )
