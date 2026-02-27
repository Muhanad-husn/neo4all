"""api/services/curation/similarity.py — Similarity and utility functions for candidate detection.

Pure-Python, deterministic helpers extracted from candidates.py (SPEC-05).
No external dependencies beyond the graph reader models.
"""

from __future__ import annotations

from collections import defaultdict

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


def _build_adjacency(rels: RelListResult) -> dict[str, set[str]]:
    """Build an undirected adjacency map: dedupe_key -> set of neighbor dedupe_keys."""
    adj: dict[str, set[str]] = defaultdict(set)
    for rel in rels.rels:
        adj[rel.start_dedupe_key].add(rel.end_dedupe_key)
        adj[rel.end_dedupe_key].add(rel.start_dedupe_key)
    return adj
