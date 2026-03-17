"""api/services/curation/dedup_pipeline.py — Pre-curation deduplication pipeline.

Runs between candidate generation and the agent pipeline, processing only
``probable_duplicate`` candidates through enrichment stages:

  1. Multi-signal score — composite scoring from collision_context + property overlap
  2. Contradiction check — property conflict detection
  3. Confidence band    — classify into high / medium / low
  4. Cluster validation — union-find coherence + safety caps

All enrichment data is passed as context to the LLM agent pipeline for
reasoned decision-making.  No deterministic merges or auto-canonicalization.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from api.cache.client import CacheClient
from api.graph.reader import GraphReader
from api.graph.reader_models import GraphNodeRecord, NodeListResult, RelListResult
from api.models.candidate import Candidate, CandidateType
from api.observability.logger import get_logger
from api.schema.models import SchemaVersion

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Governance keys excluded from property comparisons
# ---------------------------------------------------------------------------

_GOVERNANCE_KEYS: frozenset[str] = frozenset({
    "_dedupe_key",
    "_schema_version",
    "_chunk_id",
    "_primary_value",
    "run_id",
    "schema_version",
})

# ---------------------------------------------------------------------------
# Confidence band thresholds
# ---------------------------------------------------------------------------

_HIGH_THRESHOLD: float = 0.80
_MEDIUM_THRESHOLD: float = 0.60
_MAX_CLUSTER_SIZE: int = 10

# ---------------------------------------------------------------------------
# Composite score weights
# ---------------------------------------------------------------------------

# Name similarity (JW + token overlap) is the primary signal.
# Context and property overlap are supporting — their absence is normal
# for duplicates and must not drag the score below the merge threshold.
_W_JW: float = 0.50
_W_TOK: float = 0.25
_W_CJ: float = 0.15
_W_PO: float = 0.10


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------


class DedupPipelineResult:
    """Container for pipeline outputs."""

    __slots__ = ("candidates", "enriched_count")

    def __init__(self) -> None:
        self.candidates: list[Candidate] = []
        self.enriched_count: int = 0


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _property_overlap(
    props_a: dict[str, Any],
    props_b: dict[str, Any],
) -> float:
    """Ratio of matching non-governance property key-value pairs to total unique keys."""
    keys_a = set(props_a) - _GOVERNANCE_KEYS
    keys_b = set(props_b) - _GOVERNANCE_KEYS
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


def _compute_composite_score(
    ctx: dict[str, Any],
    props_a: dict[str, Any],
    props_b: dict[str, Any],
) -> float:
    """Weighted composite deduplication score.

    When context_jaccard is 0 (no shared graph neighbors), its weight is
    redistributed to jaro_winkler.  No shared neighbors is the *normal*
    state for duplicates — the duplicate exists precisely because the graph
    didn't connect them.  Absence of shared context is not evidence against
    a merge; penalizing it produces nonsensically low scores for obvious
    duplicates (e.g. JW=0.92 scoring 0.52).
    """
    jw = ctx.get("jaro_winkler", 0.0)
    tok = ctx.get("token_overlap", 0.0)
    cj = ctx.get("context_jaccard", 0.0)
    po = _property_overlap(props_a, props_b)

    # Token containment: qualitatively different from edit distance.
    # Strong with corroboration, moderate without.
    if ctx.get("containment"):
        base = 0.70
        bonus = min(cj * 0.15 + po * 0.10, 0.25)
        return base + bonus

    # Redistribute CJ weight to JW when CJ is 0 (no shared neighbors).
    if cj == 0.0:
        return (_W_JW + _W_CJ) * jw + _W_TOK * tok + _W_PO * po

    return _W_JW * jw + _W_TOK * tok + _W_CJ * cj + _W_PO * po


def _check_contradictions(
    props_a: dict[str, Any],
    props_b: dict[str, Any],
) -> list[str]:
    """Return list of conflicting non-governance property names."""
    conflicts: list[str] = []
    shared_keys = (set(props_a) & set(props_b)) - _GOVERNANCE_KEYS
    for key in sorted(shared_keys):
        va, vb = props_a.get(key), props_b.get(key)
        if va and vb and str(va).strip() != str(vb).strip():
            conflicts.append(key)
    return conflicts


def _classify_confidence(
    composite_score: float,
    has_contradictions: bool,
) -> str:
    """Map composite score + contradiction status to confidence band.

    Pure composite-score classifier — no override logic.
    """
    if composite_score >= _HIGH_THRESHOLD and not has_contradictions:
        return "high"
    if composite_score >= _MEDIUM_THRESHOLD:
        return "medium"
    if composite_score >= _HIGH_THRESHOLD and has_contradictions:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Union-find for cluster validation
# ---------------------------------------------------------------------------


class _UnionFind:
    """Minimal union-find for grouping duplicate pairs into clusters."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


# ---------------------------------------------------------------------------
# DedupPipeline
# ---------------------------------------------------------------------------


class DedupPipeline:
    """Pre-curation enrichment pipeline.

    Processes probable_duplicate candidates through scoring, contradiction
    checking, confidence banding, and cluster validation to provide rich
    context for LLM-based reasoning in the agent pipeline.

    Args:
        reader:  GraphReader for fresh graph queries after mutations.
        cache:   CacheClient for cache operations.
        run_id:  Governed run identifier.
        schema:  Locked schema version.
    """

    def __init__(
        self,
        reader: GraphReader,
        cache: CacheClient,
        run_id: str,
        schema: SchemaVersion,
    ) -> None:
        self._reader = reader
        self._cache = cache
        self._run_id = run_id
        self._schema = schema

    async def run(
        self,
        candidates: list[Candidate],
        nodes: NodeListResult,
        rels: RelListResult,
    ) -> tuple[list[Candidate], DedupPipelineResult]:
        """Enrich probable_duplicate candidates with scoring and context.

        Args:
            candidates: All candidates from the five detectors.
            nodes:      Current graph nodes.
            rels:       Current graph relationships.

        Returns:
            Tuple of (enriched_candidates, pipeline_result).
        """
        result = DedupPipelineResult()

        # Separate probable duplicates from other candidate types
        probable_dups = [
            c for c in candidates
            if c.candidate_type == CandidateType.probable_duplicate
        ]
        other_candidates = [
            c for c in candidates
            if c.candidate_type != CandidateType.probable_duplicate
        ]

        if not probable_dups:
            result.candidates = candidates
            return candidates, result

        # Build node lookup
        node_map = {n.dedupe_key: n for n in nodes.nodes}

        logger.info(
            "dedup_pipeline_start",
            run_id=self._run_id,
            probable_duplicate_count=len(probable_dups),
        )

        remaining_dups = probable_dups

        # Stages 3-6: Score, check contradictions, classify, validate clusters
        # (enrichment provides useful context for LLM reasoning)
        enriched = self._enrich_candidates(remaining_dups, node_map)

        final_candidates = other_candidates + enriched
        result.candidates = final_candidates
        result.enriched_count = len(enriched)

        logger.info(
            "dedup_pipeline_complete",
            run_id=self._run_id,
            enriched_count=len(enriched),
            total_candidates=len(final_candidates),
        )

        return final_candidates, result

    # ------------------------------------------------------------------
    # Stages 3-6: Enrich candidates
    # ------------------------------------------------------------------

    def _enrich_candidates(
        self,
        candidates: list[Candidate],
        node_map: dict[str, GraphNodeRecord],
    ) -> list[Candidate]:
        """Run stages 3-6: score, contradiction check, classify, validate clusters."""
        if not candidates:
            return []

        # Stage 3 & 4: Compute composite score and check contradictions
        for c in candidates:
            ctx = c.collision_context
            refs = list(c.involved_element_refs)
            if len(refs) >= 2:
                node_a = node_map.get(refs[0])
                node_b = node_map.get(refs[1])
                props_a = node_a.properties if node_a else {}
                props_b = node_b.properties if node_b else {}

                # Stage 3: Multi-signal score
                score = _compute_composite_score(ctx, props_a, props_b)
                ctx["composite_score"] = round(score, 4)

                # Stage 4: Contradiction checks
                conflicts = _check_contradictions(props_a, props_b)
                ctx["property_conflicts"] = conflicts
            else:
                ctx["composite_score"] = 0.0
                ctx["property_conflicts"] = []

        # Stage 5: Confidence band classification
        for c in candidates:
            ctx = c.collision_context
            score = ctx.get("composite_score", 0.0)
            has_conflicts = bool(ctx.get("property_conflicts"))
            band = _classify_confidence(score, has_conflicts)
            ctx["confidence_band"] = band

        # Stage 6: Cluster validation
        self._validate_clusters(candidates, node_map)

        return candidates

    def _validate_clusters(
        self,
        candidates: list[Candidate],
        node_map: dict[str, GraphNodeRecord],
    ) -> None:
        """Validate clusters via union-find. Downgrade incoherent clusters to medium."""
        uf = _UnionFind()
        for c in candidates:
            refs = list(c.involved_element_refs)
            if len(refs) >= 2:
                uf.union(refs[0], refs[1])

        # Build components
        all_refs: set[str] = set()
        for c in candidates:
            all_refs.update(c.involved_element_refs)

        components: dict[str, set[str]] = defaultdict(set)
        for ref in all_refs:
            root = uf.find(ref)
            components[root].add(ref)

        # Check each cluster for coherence
        incoherent_roots: set[str] = set()
        for root, members in components.items():
            # Safety cap
            if len(members) > _MAX_CLUSTER_SIZE:
                incoherent_roots.add(root)
                continue

            # Check all members are same node_type
            types_seen: set[str] = set()
            for m in members:
                node = node_map.get(m)
                if node:
                    types_seen.add(node.node_type)
            if len(types_seen) > 1:
                incoherent_roots.add(root)
                continue

            # Check no internal contradictions within the cluster
            cluster_candidates = [
                c for c in candidates
                if any(uf.find(r) == root for r in c.involved_element_refs)
            ]
            for cc in cluster_candidates:
                if cc.collision_context.get("property_conflicts"):
                    incoherent_roots.add(root)
                    break

        # Downgrade incoherent cluster members
        if incoherent_roots:
            for c in candidates:
                refs = list(c.involved_element_refs)
                if refs and uf.find(refs[0]) in incoherent_roots:
                    if c.collision_context.get("confidence_band") == "high":
                        c.collision_context["confidence_band"] = "medium"
                        c.collision_context["cluster_downgraded"] = True

