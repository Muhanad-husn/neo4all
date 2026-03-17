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

    # ----- group merge (duplicate chain) ------------------------------------
    # Synthetic group candidate created by the density-validated chain
    # conflict detector.  Confidence scales with group density and size.
    if candidate.detection_method == "duplicate_chain_group":
        survivor = ctx.get("survivor_key", "?")
        group_size = ctx.get("conflict_group_size", 0)
        density = ctx.get("edge_density", 0.0)

        # Confidence scaling based on density-validated group quality
        if group_size <= 5 and density >= 0.8:
            confidence = 0.90
            label = "Dense"
        elif group_size <= 8 and density >= 0.5:
            confidence = 0.75
            label = "Validated"
        else:
            confidence = 0.50
            label = "Sparse"

        return StructuralRecommendation(
            suggested_action="merge",
            confidence=confidence,
            reasoning=(
                f"{label} duplicate group of {group_size} overlapping nodes "
                f"(edge_density={density:.2f}). "
                f"Merge all into survivor '{survivor}' (highest degree: "
                f"{ctx.get('survivor_degree', '?')}). Individual pairwise "
                f"candidates are suppressed in favour of this atomic group merge."
            ),
        )

    # ----- suppressed pairwise (part of a chain group) --------------------
    # Pairwise candidates that belong to a chain are suppressed — the group
    # candidate handles them.  Defer so the agent pipeline skips them.
    if ctx.get("suppressed_by_group"):
        return StructuralRecommendation(
            suggested_action="defer",
            confidence=1.0,
            reasoning=(
                f"This pairwise candidate is part of a duplicate chain and "
                f"is handled by group candidate {ctx['suppressed_by_group'][:16]}…. "
                f"Deferring to avoid redundant proposals."
            ),
        )

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

    # ------------------------------------------------------------------
    # Every probable_duplicate is a MERGE.  The problem is two nodes
    # existing for the same entity.  "Canonicalize" only updates a
    # property — it does not remove the duplicate node.  Regardless of
    # whether the difference is casing, a typo, an abbreviation, or an
    # encoding variant, the answer is always merge.  The human approval
    # gate is the safety net, not a conservative heuristic here.
    # ------------------------------------------------------------------

    # Pattern 1: Case-only or encoding difference (highest confidence)
    if val_a and val_b and val_a.lower() == val_b.lower():
        return StructuralRecommendation(
            suggested_action="merge",
            confidence=min(0.95, max(jw, 0.90)),
            reasoning=(
                f"Values '{val_a}' and '{val_b}' differ only in letter "
                f"casing or encoding (similarity: {jw:.2f}). These are "
                f"the same entity — merge into canonical form."
            ),
        )

    # Pattern 1.5: Token containment (detected by containment detector, JW < 0.90)
    if ctx.get("containment"):
        shorter = val_a if len(val_a) <= len(val_b) else val_b
        longer = val_b if shorter == val_a else val_a
        if cj > 0:
            return StructuralRecommendation(
                suggested_action="merge",
                confidence=0.80,
                reasoning=(
                    f"Token containment: every word in '{shorter}' appears in "
                    f"'{longer}' (similarity: {jw:.2f}) with shared graph "
                    f"neighbours (context overlap: {cj:.2f}). Corroborated — "
                    f"merge into the more complete form."
                ),
            )
        return StructuralRecommendation(
            suggested_action="defer",
            confidence=0.55,
            reasoning=(
                f"Token containment: every word in '{shorter}' appears in "
                f"'{longer}' (similarity: {jw:.2f}) but no shared graph "
                f"neighbours. Without corroboration, '{shorter}' could refer "
                f"to a different entity — defer for review."
            ),
        )

    # Pattern 2: Near-identical (typo / formatting / encoding variant)
    if jw >= 0.95:
        conf = 0.90 if cj > 0 else 0.85
        return StructuralRecommendation(
            suggested_action="merge",
            confidence=conf,
            reasoning=(
                f"Near-identical values '{val_a}' and '{val_b}' "
                f"(similarity: {jw:.2f}). Minor formatting or "
                f"typographical difference — same entity, merge."
            ),
        )

    # Pattern 3: Name containment — one value's tokens are a subset of
    # the other's (e.g. "Ingrid Bos" vs "Ingrid Bos van Dijk").
    if val_a and val_b and jw >= 0.90:
        toks_a = set(val_a.lower().split())
        toks_b = set(val_b.lower().split())
        if toks_a != toks_b and (toks_a <= toks_b or toks_b <= toks_a):
            longer = val_a if len(toks_a) >= len(toks_b) else val_b
            shorter = val_b if longer == val_a else val_a
            if cj > 0:
                return StructuralRecommendation(
                    suggested_action="merge",
                    confidence=0.80,
                    reasoning=(
                        f"Name containment: '{shorter}' is a subset of "
                        f"'{longer}' (similarity: {jw:.2f}, word overlap: "
                        f"{tok:.2f}) with shared graph neighbours. "
                        f"Corroborated — merge into the more complete form."
                    ),
                )
            return StructuralRecommendation(
                suggested_action="defer",
                confidence=0.50,
                reasoning=(
                    f"Name containment: '{shorter}' is a subset of "
                    f"'{longer}' (similarity: {jw:.2f}, word overlap: "
                    f"{tok:.2f}) but no shared graph neighbours. Without "
                    f"corroboration this could be a different entity — "
                    f"defer for review."
                ),
            )

    # Pattern 4: High similarity (jw >= 0.90, general case)
    if jw >= 0.90:
        if cj > 0:
            conf = round(0.75 + min(cj, 0.15), 2)
            return StructuralRecommendation(
                suggested_action="merge",
                confidence=conf,
                reasoning=(
                    f"High similarity between '{val_a}' and '{val_b}' "
                    f"(similarity: {jw:.2f}) with shared graph neighbours "
                    f"(context overlap: {cj:.2f}). Same entity — merge."
                ),
            )
        if tok >= 0.8:
            return StructuralRecommendation(
                suggested_action="merge",
                confidence=0.65,
                reasoning=(
                    f"High similarity between '{val_a}' and '{val_b}' "
                    f"(similarity: {jw:.2f}, word overlap: {tok:.2f}). "
                    f"Strong token overlap supports merge."
                ),
            )
        return StructuralRecommendation(
            suggested_action="defer",
            confidence=0.45,
            reasoning=(
                f"High similarity between '{val_a}' and '{val_b}' "
                f"(similarity: {jw:.2f}, word overlap: {tok:.2f}) but no "
                f"shared graph neighbours and low token overlap. "
                f"Insufficient corroboration — defer for review."
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
    # The relationship may be semantically valid but uses a type the schema
    # doesn't define for these node types.  Normalize requires an existing
    # schema type that fits — if none exists (common case), the LLM must
    # use rename (extends schema, high-risk) or delete.  Suggest normalize
    # but at low confidence so the LLM has room to choose rename/delete.
    return StructuralRecommendation(
        suggested_action="normalize",
        confidence=0.50,
        reasoning=(
            f"Relationship '{rel_type}' connects "
            f"{ctx.get('actual_start_type', '?')} -> "
            f"{ctx.get('actual_end_type', '?')} which matches no valid "
            f"schema direction for this type. Check the schema: if an "
            f"existing relationship type connects these node types with "
            f"similar meaning, use 'normalize' with normalize_target set "
            f"to that type. If no existing type fits, use 'rename' to "
            f"create a new type (set high_risk_override=true). If the "
            f"relationship is an extraction error, use 'delete'."
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
