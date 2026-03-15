"""
ui/components/candidate_summary.py — Human-readable candidate summaries.

Pure display helpers that translate raw candidate dicts into plain-language
descriptions for the curation UI.  No business logic, no API calls.
"""

from typing import Any

# ---------------------------------------------------------------------------
# Detection method → human-readable label
# ---------------------------------------------------------------------------

_METHOD_LABELS: dict[str, str] = {
    "exact_node_dedupe_key": "Exact node duplicate",
    "exact_rel_dedupe_key": "Exact relationship duplicate",
    "canonical_inverse_violation": "Canonical inverse violation",
    "canonical_direction_violation": "Canonical direction violation",
    "orphan_node": "Orphan node",
    "degree_outlier": "Degree outlier",
    "missing_provenance_node": "Missing provenance (node)",
    "missing_provenance_rel": "Missing provenance (rel)",
    "qualifier_missing": "Missing qualifier",
    "duplicate_chain_group": "Duplicate chain",
}


def method_label(detection_method: str) -> str:
    """Human-readable label for a detection method identifier.

    Fuzzy-match methods like ``jaro_winkler_0.90`` are recognised by prefix.
    """
    if detection_method in _METHOD_LABELS:
        return _METHOD_LABELS[detection_method]
    if detection_method.startswith("jaro_winkler"):
        return "Fuzzy match"
    return detection_method.replace("_", " ").title()


# ---------------------------------------------------------------------------
# Full summary
# ---------------------------------------------------------------------------


def _node_type(ctx: dict[str, Any]) -> str:
    return ctx.get("node_type", ctx.get("label", "node"))


def _rel_type(ctx: dict[str, Any]) -> str:
    return ctx.get("rel_type", ctx.get("relationship_type", "relationship"))


def _element_count(candidate: dict[str, Any]) -> int:
    return len(candidate.get("involved_element_refs", []))


def summarize_candidate(candidate: dict[str, Any]) -> str:
    """Return a full plain-language summary for a candidate dict."""
    method = candidate.get("detection_method", "")
    ctx = candidate.get("collision_context", {})
    count = _element_count(candidate)

    if method == "exact_node_dedupe_key":
        nt = _node_type(ctx)
        return f"{count} exact duplicate {nt} node(s) found"

    if method == "exact_rel_dedupe_key":
        rt = _rel_type(ctx)
        return f"{count} exact duplicate {rt} relationship(s) found"

    if method.startswith("jaro_winkler"):
        # Extract similarity score from method name (e.g. jaro_winkler_0.90)
        parts = method.split("_")
        score_str = ""
        if len(parts) >= 3:
            try:
                score_str = f" ({int(float(parts[-1]) * 100)}% similar)"
            except (ValueError, IndexError):
                pass
        nt = _node_type(ctx)
        names = [str(v) for v in ctx.get("names", ctx.get("values", []))]
        if len(names) >= 2:
            return (
                f"Possible duplicate {nt} nodes: "
                f"'{names[0]}' vs '{names[1]}'{score_str}"
            )
        return f"Possible duplicate {nt} nodes{score_str}"

    if method == "canonical_inverse_violation":
        rt = _rel_type(ctx)
        src = ctx.get("start_label", "?")
        tgt = ctx.get("end_label", "?")
        exp_src = ctx.get("expected_start", "?")
        exp_tgt = ctx.get("expected_end", "?")
        return (
            f"{rt} goes {src} \u2192 {tgt}, expected {exp_src} \u2192 {exp_tgt}"
        )

    if method == "canonical_direction_violation":
        rt = _rel_type(ctx)
        src = ctx.get("start_label", "?")
        tgt = ctx.get("end_label", "?")
        return f"{rt} connects wrong types: {src} \u2192 {tgt}"

    if method == "orphan_node":
        return "Orphan node with no connections"

    if method == "degree_outlier":
        degree = ctx.get("degree", "?")
        avg = ctx.get("average_degree", "?")
        return f"Node has {degree} connections (average ~{avg})"

    if method == "missing_provenance_node":
        nt = _node_type(ctx)
        field = ctx.get("missing_field", "schema_version")
        return f"{nt} node missing required field: {field}"

    if method == "missing_provenance_rel":
        rt = _rel_type(ctx)
        field = ctx.get("missing_field", "schema_version")
        return f"{rt} relationship missing required field: {field}"

    if method == "qualifier_missing":
        nt = _node_type(ctx)
        qualifier = ctx.get("qualifier", ctx.get("missing_field", "unknown"))
        return f"{nt} node missing qualifier: {qualifier}"

    if method == "duplicate_chain_group":
        chain_len = ctx.get("chain_length", count)
        return f"Chain of {chain_len} connected duplicates \u2014 merge group"

    # Fallback
    return f"Candidate detected by {method_label(method).lower()}"


# ---------------------------------------------------------------------------
# Short summary (< 60 chars, for dropdown labels)
# ---------------------------------------------------------------------------


def short_summary(candidate: dict[str, Any]) -> str:
    """Compact summary suitable for dropdown labels (< 60 chars)."""
    method = candidate.get("detection_method", "")
    ctx = candidate.get("collision_context", {})
    count = _element_count(candidate)

    if method == "exact_node_dedupe_key":
        nt = _node_type(ctx)
        return f"{count} exact dup {nt}"

    if method == "exact_rel_dedupe_key":
        rt = _rel_type(ctx)
        return f"{count} exact dup {rt} rel"

    if method.startswith("jaro_winkler"):
        nt = _node_type(ctx)
        names = [str(v) for v in ctx.get("names", ctx.get("values", []))]
        if len(names) >= 2:
            a = names[0][:15]
            b = names[1][:15]
            return f"Fuzzy: '{a}' vs '{b}'"
        return f"Fuzzy dup {nt}"

    if method == "canonical_inverse_violation":
        rt = _rel_type(ctx)
        return f"Inverse violation: {rt}"

    if method == "canonical_direction_violation":
        rt = _rel_type(ctx)
        return f"Direction violation: {rt}"

    if method == "orphan_node":
        return "Orphan node"

    if method == "degree_outlier":
        degree = ctx.get("degree", "?")
        return f"Degree outlier ({degree} connections)"

    if method == "missing_provenance_node":
        nt = _node_type(ctx)
        return f"Missing provenance: {nt}"

    if method == "missing_provenance_rel":
        rt = _rel_type(ctx)
        return f"Missing provenance: {rt} rel"

    if method == "qualifier_missing":
        nt = _node_type(ctx)
        qualifier = ctx.get("qualifier", ctx.get("missing_field", "?"))
        return f"{nt} missing: {qualifier}"

    if method == "duplicate_chain_group":
        chain_len = ctx.get("chain_length", count)
        return f"Dup chain ({chain_len} nodes)"

    label = method_label(method).lower()
    return label[:58]
