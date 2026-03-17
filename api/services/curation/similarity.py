"""api/services/curation/similarity.py — Similarity and utility functions for candidate detection.

Pure-Python, deterministic helpers extracted from candidates.py (SPEC-05).
No external dependencies beyond the graph reader models.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from api.graph.reader_models import GraphNodeRecord, RelListResult


# ---------------------------------------------------------------------------
# Jaro-Winkler — pure Python, deterministic, no external library
# ---------------------------------------------------------------------------


def _jaro(s: str, t: str) -> float:
    """Compute Jaro similarity between two strings."""
    if s == t:
        return 1.0
    ls, lt = len(s), len(t)
    if not ls or not lt:
        return 0.0
    window = max(max(ls, lt) // 2 - 1, 0)
    s_m: list[bool] = [False] * ls
    t_m: list[bool] = [False] * lt
    matches = 0
    for i in range(ls):
        lo, hi = max(0, i - window), min(i + window + 1, lt)
        for j in range(lo, hi):
            if not t_m[j] and s[i] == t[j]:
                s_m[i] = t_m[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    trans = k = 0
    for i in range(ls):
        if not s_m[i]:
            continue
        while not t_m[k]:
            k += 1
        if s[i] != t[k]:
            trans += 1
        k += 1
    return (matches / ls + matches / lt + (matches - trans / 2) / matches) / 3.0


def _jaro_winkler(s: str, t: str, p: float = 0.1) -> float:
    """Jaro-Winkler similarity with common-prefix bonus (max prefix 4, scale p)."""
    jaro = _jaro(s, t)
    prefix = 0
    for a, b in zip(s[:4], t[:4]):
        if a == b:
            prefix += 1
        else:
            break
    return jaro + prefix * p * (1.0 - jaro)


# ---------------------------------------------------------------------------
# Token overlap
# ---------------------------------------------------------------------------


def _token_overlap(s: str, t: str) -> float:
    """Token-level overlap ratio between two strings.

    Tokenises on whitespace + punctuation boundaries, lowercases, then
    computes ``2 * |intersection| / (|tokens_a| + |tokens_b|)``.
    Returns 0.0 if both strings are empty.
    """
    import re as _re

    _TOKEN_RE = _re.compile(r"[a-z0-9]+")
    tokens_a = set(_TOKEN_RE.findall(s.lower()))
    tokens_b = set(_TOKEN_RE.findall(t.lower()))
    total = len(tokens_a) + len(tokens_b)
    if total == 0:
        return 0.0
    return 2.0 * len(tokens_a & tokens_b) / total


# ---------------------------------------------------------------------------
# Token containment
# ---------------------------------------------------------------------------


# Stopwords filtered from token containment and token-blocking passes.
# These are function words that appear in most English phrases and cause
# false containment matches between unrelated long descriptions.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for",
    "from", "had", "has", "have", "he", "her", "his", "how", "i",
    "if", "in", "into", "is", "it", "its", "may", "my", "no", "nor",
    "not", "of", "on", "or", "our", "out", "own", "per", "she", "so",
    "than", "that", "the", "their", "them", "then", "there", "these",
    "they", "this", "to", "too", "up", "us", "was", "we", "were",
    "what", "when", "which", "who", "why", "will", "with", "would",
    "you", "your",
})


def _content_tokens(s: str) -> set[str]:
    """Extract content tokens (no stopwords, no single chars) from a string.

    Pure, deterministic.  Used by _token_containment and the token-blocking
    pass in ProbableDuplicateDetector.
    """
    import re as _re
    _TOKEN_RE = _re.compile(r"[a-z0-9]+")
    return {t for t in _TOKEN_RE.findall(s.lower()) if t not in _STOPWORDS and len(t) > 1}


def _token_containment(s: str, t: str, min_short_len: int = 3) -> bool:
    """True when every content token of the shorter string appears in the longer.

    Guards:
      - Stopwords and single-character tokens are excluded from comparison.
      - The shorter string must have >= 2 content tokens.
      - The shorter string must be >= *min_short_len* characters.
      - Token sets must not be equal (handled by JW / auto-canonical).

    Pure, deterministic, no external deps.
    """
    tokens_s = _content_tokens(s)
    tokens_t = _content_tokens(t)
    if not tokens_s or not tokens_t:
        return False
    # Reject equal token sets — handled elsewhere.
    if tokens_s == tokens_t:
        return False
    # Identify shorter / longer by token count (break ties by char length).
    if len(tokens_s) < len(tokens_t):
        shorter_tokens, longer_tokens, shorter_str = tokens_s, tokens_t, s
    elif len(tokens_s) > len(tokens_t):
        shorter_tokens, longer_tokens, shorter_str = tokens_t, tokens_s, t
    else:
        # Same token count but different sets — not a containment relationship.
        return False
    # Guard: shorter must have at least 1 content token.
    if len(shorter_tokens) < 1:
        return False
    # Guard: shorter string must meet minimum character length.
    if len(shorter_str.strip()) < min_short_len:
        return False
    # Every content token in the shorter must appear in the longer.
    return shorter_tokens <= longer_tokens


# ---------------------------------------------------------------------------
# Set similarity
# ---------------------------------------------------------------------------


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity of two sets.  Returns 0.0 if both are empty."""
    union = a | b
    return len(a & b) / len(union) if union else 0.0


# ---------------------------------------------------------------------------
# Node / relationship helpers
# ---------------------------------------------------------------------------


def _primary_value(node: GraphNodeRecord, prop: str) -> str:
    """Primary property value as lower-cased, stripped string (empty if absent).

    Checks the schema-defined property name first (e.g. ``"name"``), then
    falls back to the ``_primary_value`` governance property that the graph
    writer always sets during extraction.  This handles the common case where
    the LLM extraction stores the entity name as ``ExtractedNode.primary_value``
    (written to Neo4j as ``_primary_value``) rather than duplicating it inside
    the ``properties`` dict under the schema key.
    """
    raw = node.properties.get(prop)
    if raw is None:
        raw = node.properties.get("_primary_value")
    return str(raw).lower().strip() if raw is not None else ""


def _property_overlap(
    props_a: dict[str, Any],
    props_b: dict[str, Any],
    governance_keys: set[str] | None = None,
) -> float:
    """Ratio of matching non-governance property key-value pairs to total unique keys.

    Compares non-governance, non-primary properties.  Returns 0.0 when both
    property dicts have no comparable keys.
    """
    if governance_keys is None:
        governance_keys = {
            "_dedupe_key", "_schema_version", "_chunk_id",
            "_primary_value", "run_id", "schema_version",
        }
    keys_a = set(props_a) - governance_keys
    keys_b = set(props_b) - governance_keys
    all_keys = keys_a | keys_b
    if not all_keys:
        return 0.0
    matching = 0
    for key in all_keys:
        va = props_a.get(key)
        vb = props_b.get(key)
        if va is not None and vb is not None and str(va).strip() == str(vb).strip():
            matching += 1
    return matching / len(all_keys)


def _build_adjacency(rels: RelListResult) -> dict[str, set[str]]:
    """Build an undirected adjacency map: dedupe_key -> set of neighbor dedupe_keys."""
    adj: dict[str, set[str]] = defaultdict(set)
    for rel in rels.rels:
        adj[rel.start_dedupe_key].add(rel.end_dedupe_key)
        adj[rel.end_dedupe_key].add(rel.start_dedupe_key)
    return adj
