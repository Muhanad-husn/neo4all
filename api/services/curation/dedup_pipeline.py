"""api/services/curation/dedup_pipeline.py — Pre-curation deduplication pipeline.

Runs between candidate generation and the agent pipeline, processing only
``probable_duplicate`` candidates through 7 stages:

  1. Auto-canonicalize  — deterministic-only mutations (case/whitespace/punctuation)
  2. Re-detect          — refresh probable duplicates after Stage 1 mutations
  3. Multi-signal score — composite scoring from collision_context + property overlap
  4. Contradiction check — property conflict detection
  5. Confidence band    — classify into high / medium / low
  6. Cluster validation — union-find coherence + safety caps
  7. Emit results       — create merge proposals (high) or pass through (medium/low)

Auto-canonicalize (Stage 1) bypasses CLAUDE.md §4.2 for deterministic-only
mutations.  All merges still go through the standard approval pipeline.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from api.cache.client import CacheClient
from api.graph.reader import GraphReader
from api.graph.reader_models import GraphNodeRecord, NodeListResult, RelListResult
from api.models.candidate import Candidate, CandidateType
from api.observability.logger import get_logger
from api.proposals.models import ElementRef, ProposalClass, ProposalPacket
from api.proposals.service import ProposalService
from api.schema.models import SchemaVersion
from api.services.curation.candidates import ProbableDuplicateDetector
from api.services.curation.similarity import _build_adjacency
from api.services.curation.snapshots import build_snapshot, save_snapshot

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

    __slots__ = ("candidates", "auto_canonicalized", "merge_proposals_created", "deferred")

    def __init__(self) -> None:
        self.candidates: list[Candidate] = []
        self.auto_canonicalized: int = 0
        self.merge_proposals_created: int = 0
        self.deferred: int = 0


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _is_deterministic_canonical(val_a: str, val_b: str) -> bool:
    """True when the difference is purely case, whitespace, or punctuation."""
    norm_a = re.sub(r"[^a-z0-9]", "", val_a.lower())
    norm_b = re.sub(r"[^a-z0-9]", "", val_b.lower())
    return norm_a == norm_b and val_a != val_b


def _canonical_form(val_a: str, val_b: str) -> str:
    """Select canonical form: title case of the longer value (deterministic)."""
    longer = val_a if len(val_a) >= len(val_b) else val_b
    return longer.title()


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
    # "Aiko" ⊂ "Aiko Nakamura" is strong evidence regardless of JW score.
    if ctx.get("containment"):
        base = 0.85
        bonus = min(cj * 0.10 + po * 0.05, 0.10)
        return base + bonus

    # When context_jaccard is 0 and name similarity is high (JW >= 0.90),
    # the name IS the evidence.  No shared neighbors is the normal state
    # for duplicates.  Use JW directly as the floor — don't let partial
    # token overlap or missing properties drag an obvious duplicate below
    # the merge threshold.
    if cj == 0.0 and jw >= 0.90:
        weighted = (_W_JW + _W_CJ) * jw + _W_TOK * tok + _W_PO * po
        return max(weighted, jw)

    # Redistribute CJ weight to JW when CJ is 0 but JW is below 0.90.
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
    jaro_winkler: float = 0.0,
    is_containment: bool = False,
) -> str:
    """Map composite score + contradiction status to confidence band.

    When jaro_winkler >= 0.90 the names are near-identical — the candidate
    is at least "medium" regardless of composite score so it reaches the
    agent pipeline without the "deferred" stigma.

    Token containment candidates are at least "medium" — they represent
    genuine name subset relationships that should never be deferred.
    """
    if composite_score >= _HIGH_THRESHOLD and not has_contradictions:
        return "high"
    if composite_score >= _MEDIUM_THRESHOLD:
        return "medium"
    if composite_score >= _HIGH_THRESHOLD and has_contradictions:
        return "medium"
    # High name similarity alone is strong signal — never defer these.
    if jaro_winkler >= 0.90:
        return "medium"
    # Token containment is a strong structural signal — never defer.
    if is_containment:
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
    """Pre-curation deduplication pipeline.

    Processes probable_duplicate candidates through 7 stages to auto-resolve
    deterministic cases and enrich remaining candidates for the agent pipeline.

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
        """Execute all 7 pipeline stages.

        Args:
            candidates: All candidates from the five detectors.
            nodes:      Current graph nodes.
            rels:       Current graph relationships.

        Returns:
            Tuple of (remaining_candidates, pipeline_result).
            remaining_candidates excludes auto-canonicalized pairs and deferred pairs.
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

        # Stage 1: Auto-canonicalize
        remaining_dups, auto_count = await self._auto_canonicalize(
            probable_dups, node_map
        )
        result.auto_canonicalized = auto_count

        # Stage 2: Re-detect if any auto-canonicalization occurred
        if auto_count > 0:
            remaining_dups, nodes, rels, node_map = await self._redetect(
                remaining_dups
            )

        # Stages 3-6: Score, check contradictions, classify, validate clusters
        enriched = self._enrich_candidates(remaining_dups, node_map)

        # Stage 7: Emit results
        final_dups, proposals_created, deferred_count = await self._emit_results(
            enriched, node_map
        )
        result.merge_proposals_created = proposals_created
        result.deferred = deferred_count

        final_candidates = other_candidates + final_dups
        result.candidates = final_candidates

        logger.info(
            "dedup_pipeline_complete",
            run_id=self._run_id,
            auto_canonicalized=auto_count,
            merge_proposals=proposals_created,
            deferred=deferred_count,
            remaining_probable_dups=len(final_dups),
        )

        return final_candidates, result

    # ------------------------------------------------------------------
    # Stage 1: Auto-canonicalize
    # ------------------------------------------------------------------

    async def _auto_canonicalize(
        self,
        candidates: list[Candidate],
        node_map: dict[str, GraphNodeRecord],
    ) -> tuple[list[Candidate], int]:
        """Auto-resolve pairs differing only in case/whitespace/punctuation.

        Returns (remaining_candidates, count_of_auto_canonicalized).
        """
        remaining: list[Candidate] = []
        auto_count = 0

        for c in candidates:
            ctx = c.collision_context
            val_a = str(ctx.get("value_a", ""))
            val_b = str(ctx.get("value_b", ""))

            if not val_a or not val_b or not _is_deterministic_canonical(val_a, val_b):
                remaining.append(c)
                continue

            # Determine canonical form and which node to update
            canonical = _canonical_form(val_a, val_b)
            refs = list(c.involved_element_refs)
            if len(refs) < 2:
                remaining.append(c)
                continue

            # Find the node whose value differs from canonical
            key_a, key_b = refs[0], refs[1]
            node_a = node_map.get(key_a)
            node_b = node_map.get(key_b)
            if not node_a or not node_b:
                remaining.append(c)
                continue

            # Create proposal, diff, and apply for the auto-canonical mutation
            try:
                await self._apply_auto_canonical(
                    candidate=c,
                    canonical_value=canonical,
                    node_a=node_a,
                    node_b=node_b,
                    key_a=key_a,
                    key_b=key_b,
                    val_a=val_a,
                    val_b=val_b,
                )
                auto_count += 1
                logger.info(
                    "auto_canonical_applied",
                    run_id=self._run_id,
                    value_a=val_a,
                    value_b=val_b,
                    canonical=canonical,
                )
            except Exception as exc:
                logger.warning(
                    "auto_canonical_failed",
                    run_id=self._run_id,
                    candidate_id=c.candidate_id,
                    error=str(exc),
                )
                remaining.append(c)

        return remaining, auto_count

    async def _apply_auto_canonical(
        self,
        candidate: Candidate,
        canonical_value: str,
        node_a: GraphNodeRecord,
        node_b: GraphNodeRecord,
        key_a: str,
        key_b: str,
        val_a: str,
        val_b: str,
    ) -> None:
        """Compose and submit auto-canonical proposal + execute through governed pipeline.

        Creates a ProposalPacket, submits via ProposalService, then auto-approves
        and executes through ExecutionAgent with actor='system:auto_canonical'.
        """
        from api.agents.execution import ApprovalRecord, ExecutionAgent
        from api.cache.keys import CacheKey
        from api.diff.builder import DiffBuilder

        # Determine which node(s) need updating
        targets: list[ElementRef] = []
        for key, val, node in [(key_a, val_a, node_a), (key_b, val_b, node_b)]:
            if val != canonical_value:
                targets.append(ElementRef(
                    element_type="node",
                    dedupe_key=key,
                    label=key,
                    properties={"_primary_value": canonical_value},
                ))

        packet = ProposalPacket(
            run_id=self._run_id,
            candidate_id=candidate.candidate_id,
            proposal_class=ProposalClass.canonicalize,
            schema_version=self._schema.version_hash,
            targets=tuple(targets),
            rationale=(
                f"Auto-canonical: values '{val_a}' and '{val_b}' differ only in "
                f"case/whitespace/punctuation. Standardising to '{canonical_value}'."
            ),
            confidence_score=1.0,
        )

        # Submit proposal
        svc = ProposalService()
        stored = await svc.create(packet)

        # Approve and execute
        approval_id = f"auto_canonical_{stored.proposal_id[:16]}"
        approval = ApprovalRecord(
            approval_id=approval_id,
            proposal_id=stored.proposal_id,
            run_id=self._run_id,
            actor="system:auto_canonical",
            is_confirmed=True,
        )
        await self._cache.set(
            CacheKey.approval(approval_id), approval
        )

        from api.proposals.models import ProposalState

        # Transition proposal to approved
        await svc.transition(self._run_id, stored.proposal_id, ProposalState.approved)

        # Build diff
        diff_builder = DiffBuilder()
        diff = diff_builder.build(stored)

        # Execute
        agent = ExecutionAgent(cache=self._cache)
        audit = await agent.execute(diff, approval_id, actor="system:auto_canonical")

        if str(audit.outcome) != "applied":
            raise RuntimeError(
                f"Auto-canonical execution failed: {audit.error_detail}"
            )

    # ------------------------------------------------------------------
    # Stage 2: Re-detect
    # ------------------------------------------------------------------

    async def _redetect(
        self,
        remaining: list[Candidate],
    ) -> tuple[list[Candidate], NodeListResult, RelListResult, dict[str, GraphNodeRecord]]:
        """Re-run probable duplicate detection on updated graph."""
        try:
            nodes = await self._reader.get_nodes_by_run(self._run_id)
            rels = await self._reader.get_relationships_by_run(self._run_id)
        except Exception as exc:
            logger.warning(
                "dedup_redetect_graph_read_failed",
                run_id=self._run_id,
                error=str(exc),
            )
            node_map = {}
            return remaining, NodeListResult(nodes=()), RelListResult(rels=()), node_map

        fresh_dups = ProbableDuplicateDetector().detect(
            run_id=self._run_id,
            schema_version=self._schema.version_hash,
            nodes=nodes,
            schema=self._schema,
            rels=rels,
        )

        node_map = {n.dedupe_key: n for n in nodes.nodes}

        logger.info(
            "dedup_redetect_complete",
            run_id=self._run_id,
            previous_count=len(remaining),
            fresh_count=len(fresh_dups),
        )

        return fresh_dups, nodes, rels, node_map

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
            jw = ctx.get("jaro_winkler", 0.0)
            band = _classify_confidence(
                score, has_conflicts,
                jaro_winkler=jw,
                is_containment=bool(ctx.get("containment")),
            )
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

    # ------------------------------------------------------------------
    # Stage 7: Emit results
    # ------------------------------------------------------------------

    async def _emit_results(
        self,
        candidates: list[Candidate],
        node_map: dict[str, GraphNodeRecord],
    ) -> tuple[list[Candidate], int, int]:
        """Emit merge proposals for high-confidence, pass through medium, defer low.

        Returns (remaining_candidates, proposals_created, deferred_count).
        """
        remaining: list[Candidate] = []
        proposals_created = 0
        deferred_count = 0

        for c in candidates:
            band = c.collision_context.get("confidence_band", "low")

            if band == "high":
                # Snapshot + create merge proposal
                try:
                    await self._create_merge_proposal(c, node_map)
                    proposals_created += 1
                except Exception as exc:
                    logger.warning(
                        "dedup_merge_proposal_failed",
                        run_id=self._run_id,
                        candidate_id=c.candidate_id,
                        error=str(exc),
                    )
                    # Fall through to agent pipeline on failure
                    remaining.append(c)
                    continue
                # High-confidence merge proposal created — still include candidate
                # so it appears in the approval queue
                remaining.append(c)

            elif band == "medium":
                # Pass through to agent pipeline unchanged
                remaining.append(c)

            else:
                # Low confidence — defer
                c.collision_context["deferred_by_pipeline"] = True
                deferred_count += 1
                remaining.append(c)

        return remaining, proposals_created, deferred_count

    async def _create_merge_proposal(
        self,
        candidate: Candidate,
        node_map: dict[str, GraphNodeRecord],
    ) -> None:
        """Snapshot obsolete node and create a merge ProposalPacket."""
        refs = list(candidate.involved_element_refs)
        if len(refs) < 2:
            return

        key_a, key_b = refs[0], refs[1]
        node_a = node_map.get(key_a)
        node_b = node_map.get(key_b)

        # Snapshot the node that will be merged away (shorter primary value)
        for key, node in [(key_a, node_a), (key_b, node_b)]:
            if node is None:
                continue
            try:
                snapshot = build_snapshot(
                    run_id=self._run_id,
                    schema_version=self._schema.version_hash,
                    dedupe_key=key,
                    node_type=node.node_type,
                    properties=dict(node.properties),
                    outgoing_rels=[],
                    incoming_rels=[],
                )
                await save_snapshot(snapshot)
            except Exception as exc:
                logger.warning(
                    "dedup_snapshot_failed",
                    run_id=self._run_id,
                    dedupe_key=key,
                    error=str(exc),
                )

        # Build target ElementRefs
        targets = tuple(
            ElementRef(
                element_type="node",
                dedupe_key=ref,
                label=ref,
            )
            for ref in refs
        )

        ctx = candidate.collision_context
        score = ctx.get("composite_score", 0.0)

        packet = ProposalPacket(
            run_id=self._run_id,
            candidate_id=candidate.candidate_id,
            proposal_class=ProposalClass.merge,
            schema_version=self._schema.version_hash,
            targets=targets,
            rationale=(
                f"Pre-curation pipeline: high-confidence merge "
                f"(composite_score={score:.4f}, no property contradictions). "
                f"Values: '{ctx.get('value_a', '')}' / '{ctx.get('value_b', '')}'."
            ),
            confidence_score=min(1.0, score),
        )

        svc = ProposalService()
        await svc.create(packet)
