"""
api/routers/candidates.py — Candidate generation & listing endpoints (SPEC-05 S-05.4).

Candidate generation lifecycle:
  POST /candidates/generate  — Run all five deterministic detectors, cache results.
  GET  /candidates/{run_id}  — Return cached candidates grouped by type.

Architecture (SKILL-B: thin routers)
--------------------------------------
All detection logic lives in api/services/curation/candidates.py.
All graph reads are delegated to api/graph/reader.py (GraphReader).
Route handlers coordinate calls, map errors to HTTP status codes, and
build response models.  No detector logic in this file.

Error handling
--------------
POST /candidates/generate:
  409 — no locked schema for this run_id (approve Phase 1 first).
  503 — Neo4j unavailable; graph read failed.
GET /candidates/{run_id}:
  Always HTTP 200.  Returns empty groups if generation has not yet been
  triggered or the 5-minute cache TTL has expired.

Caching (SKILL-D R-D8)
-----------------------
Candidate results cached under CacheKey.candidates(run_id, detection_hash)
with 5-minute TTL.  After Agent-C execution, run-scoped cache is invalidated
automatically (SKILL-D R-D10) inside ExecutionAgent.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.graph.reader import GraphReader, get_graph_reader
from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity
from api.models.responses import BaseResponse, ErrorDetail
from api.observability.logger import get_logger
from api.schema.models import SchemaVersion
from api.services.curation.candidates import (
    CanonicalViolationDetector,
    ExactNodeDuplicateDetector,
    ExactRelDuplicateDetector,
    ProbableDuplicateDetector,
    StructuralAnomalyDetector,
    run_scoped_detection,
)
from api.schema.service import get_schema_service

logger = get_logger(__name__)

router = APIRouter(tags=["candidates"])

# 5-minute TTL for candidate results (SKILL-D R-D8)
_CANDIDATE_CACHE_TTL: int = 300

# Severity sort order: critical first.
_SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ---------------------------------------------------------------------------
# Cache key helper
# ---------------------------------------------------------------------------


def _detection_hash(schema_version: str, stage: int | None = None) -> str:
    """Deterministic 32-char hash of detection parameters.

    Encodes schema_version + "all_detectors_v1" sentinel (plus optional
    stage tag) so that:
    - The same schema + stage always produces the same hash.
    - Different stages produce different cache keys.

    Returns the first 32 hex chars of the SHA-256 digest.
    """
    stage_tag = f"_stage{stage}" if stage is not None else ""
    payload = f"{schema_version}:all_detectors_v1{stage_tag}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Request / Response models (SKILL-A R-A1, R-A2)
# ---------------------------------------------------------------------------


class GenerateCandidatesRequest(BaseModel):
    """Request body for POST /api/curation/candidates/generate.

    Attributes:
        run_id: Governed run to detect candidates for.
        stage:  Optional staged detection pass.
                1 = duplicates (exact node/rel + probable),
                2 = canonical violations,
                3 = structural anomalies.
                None = all detectors (backward compatible).
    """

    run_id: str
    stage: int | None = None


class GenerateScopedCandidatesRequest(BaseModel):
    """Request body for POST /api/curation/candidates/generate-scoped."""

    run_id: str
    affected_node_keys: list[str]


class CandidateTypeCount(BaseModel):
    """Candidate count for a single detector type."""

    candidate_type: str
    count: int


class GenerateCandidatesResponse(BaseResponse):
    """Response for POST /api/curation/candidates/generate.

    Attributes:
        total_count:     Total candidates detected across all five detectors.
        counts_by_type:  Per-detector-type breakdown, in CandidateType order.
        schema_version:  version_hash of the locked schema used for detection.
    """

    total_count: int = 0
    counts_by_type: list[CandidateTypeCount] = []
    schema_version: str = ""


class CandidateOut(BaseModel):
    """Serializable representation of a Candidate for API responses and cache.

    All enum fields are serialised as str to avoid leaking internal enum
    types at the response boundary (SKILL-A R-A3).
    involved_element_refs is a list (JSON-native) rather than a tuple.
    """

    candidate_id: str
    run_id: str
    schema_version: str
    candidate_type: str
    candidate_lane: str
    involved_element_refs: list[str]
    severity: str
    detection_method: str
    collision_context: dict[str, Any]


class CandidateGroup(BaseModel):
    """Grouped candidate collection for a single CandidateType.

    Attributes:
        candidate_type:   The detector type that produced this group.
        lane:             Candidate lane (node | relationship | structural).
        total:            Total candidates in this group.
        severity_counts:  Count per severity level (critical, high, medium, low).
        candidates:       Candidates sorted by severity (critical first) then id.
    """

    candidate_type: str
    lane: str
    total: int
    severity_counts: dict[str, int]
    candidates: list[CandidateOut]


class ListCandidatesResponse(BaseResponse):
    """Response for GET /api/curation/candidates/{run_id}.

    Attributes:
        total_count:   Total candidates across all groups.
        groups:        Candidates grouped by type, in CandidateType enum order.
        schema_version: version_hash of the locked schema, or "" if unknown.
    """

    total_count: int = 0
    groups: list[CandidateGroup] = []
    schema_version: str = ""


# ---------------------------------------------------------------------------
# Cache envelope — internal schema, not exposed via API
# ---------------------------------------------------------------------------


class _CandidateListCache(BaseModel):
    """Redis cache envelope for a run's candidate list."""

    candidates: list[CandidateOut]
    schema_version: str


class _ExcludedCandidatesCache(BaseModel):
    """Redis cache envelope for the set of excluded candidate_ids within a run."""

    candidate_ids: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_candidate_out(c: Candidate) -> CandidateOut:
    """Convert an internal Candidate to its API-safe CandidateOut form."""
    return CandidateOut(
        candidate_id=c.candidate_id,
        run_id=c.run_id,
        schema_version=c.schema_version,
        candidate_type=str(c.candidate_type),
        candidate_lane=str(c.candidate_lane),
        involved_element_refs=list(c.involved_element_refs),
        severity=str(c.severity),
        detection_method=c.detection_method,
        collision_context=dict(c.collision_context),
    )


def _build_type_counts(candidates: list[CandidateOut]) -> list[CandidateTypeCount]:
    """Build per-type count list ordered by CandidateType enum definition."""
    counts: Counter[str] = Counter(c.candidate_type for c in candidates)
    return [
        CandidateTypeCount(candidate_type=t.value, count=counts[t.value])
        for t in CandidateType
        if counts[t.value] > 0
    ]


def _build_groups(
    run_id: str,
    candidates: list[CandidateOut],
    schema_version: str,
) -> ListCandidatesResponse:
    """Group a flat candidate list by type and build a ListCandidatesResponse.

    Ordering:
    - Groups follow CandidateType enum definition order.
    - Within each group, candidates are sorted by severity ascending
      (critical -> high -> medium -> low), then by candidate_id for
      deterministic tie-breaking.
    """
    by_type: dict[str, list[CandidateOut]] = defaultdict(list)
    lane_by_type: dict[str, str] = {}

    for c in candidates:
        by_type[c.candidate_type].append(c)
        lane_by_type[c.candidate_type] = c.candidate_lane

    groups: list[CandidateGroup] = []
    for ctype in (t.value for t in CandidateType):
        if ctype not in by_type:
            continue
        members = sorted(
            by_type[ctype],
            key=lambda x: (_SEVERITY_ORDER.get(x.severity, 99), x.candidate_id),
        )
        sev_counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for m in members:
            if m.severity in sev_counts:
                sev_counts[m.severity] += 1
        groups.append(
            CandidateGroup(
                candidate_type=ctype,
                lane=lane_by_type.get(ctype, ""),
                total=len(members),
                severity_counts=sev_counts,
                candidates=members,
            )
        )

    return ListCandidatesResponse(
        run_id=run_id,
        status="success",
        total_count=len(candidates),
        groups=groups,
        schema_version=schema_version,
    )


# ---------------------------------------------------------------------------
# Union-find for duplicate chain conflict detection
# ---------------------------------------------------------------------------


class _UnionFind:
    """Minimal union-find (disjoint set) for grouping duplicate chain nodes."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self._parent:
            self._parent[x] = x
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # path compression
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def _pick_survivor(
    members: set[str],
    degree_map: dict[str, int],
) -> str:
    """Pick the most-connected node in a component as merge survivor.

    Ties are broken by lexicographic sort of dedupe_key for determinism.
    """
    return max(
        sorted(members),  # sort first for deterministic tie-breaking
        key=lambda k: degree_map.get(k, 0),
    )


def _annotate_conflict_groups(
    candidates: list[Candidate],
    degree_map: dict[str, int] | None = None,
) -> list[Candidate]:
    """Detect duplicate chains and create synthetic group-merge candidates.

    For each connected component with >2 nodes:
    1. Create a synthetic group candidate (detection_method=duplicate_chain_group)
       with all component nodes in involved_element_refs, survivor first.
    2. Mark pairwise candidates in the chain as suppressed
       (collision_context["suppressed_by_group"] = group candidate_id).

    Returns the list of newly-created group candidates (caller appends them).
    Mutates collision_context dicts in-place on existing candidates.
    """
    if degree_map is None:
        degree_map = {}

    duplicate_types = {
        CandidateType.exact_node_duplicate,
        CandidateType.exact_rel_duplicate,
        CandidateType.probable_duplicate,
    }

    # Collect only duplicate candidates with >=2 refs (pairwise).
    dup_candidates = [
        c for c in candidates
        if c.candidate_type in duplicate_types and len(c.involved_element_refs) >= 2
    ]
    if not dup_candidates:
        return []

    # Build union-find from ref pairs.
    uf = _UnionFind()
    for c in dup_candidates:
        refs = list(c.involved_element_refs)
        for i in range(len(refs) - 1):
            uf.union(refs[i], refs[i + 1])

    # Find component sizes.
    components: dict[str, set[str]] = defaultdict(set)
    all_refs: set[str] = set()
    for c in dup_candidates:
        all_refs.update(c.involved_element_refs)
    for ref in all_refs:
        root = uf.find(ref)
        components[root].add(ref)

    # Identify chain components (>2 nodes).
    chain_components: dict[str, set[str]] = {
        root: members
        for root, members in components.items()
        if len(members) > 2
    }

    if not chain_components:
        return []

    # Build a group candidate for each chain component.
    # Use the first pairwise candidate in the chain for run_id / schema_version.
    first_dup = dup_candidates[0]
    group_candidates: list[Candidate] = []

    for root, members in chain_components.items():
        survivor = _pick_survivor(members, degree_map)
        # Order: survivor first, rest sorted for determinism.
        ordered_refs = (survivor,) + tuple(sorted(members - {survivor}))

        group = Candidate(
            run_id=first_dup.run_id,
            schema_version=first_dup.schema_version,
            candidate_type=CandidateType.probable_duplicate,
            candidate_lane=CandidateLane.node,
            involved_element_refs=ordered_refs,
            severity=Severity.high,
            detection_method="duplicate_chain_group",
            collision_context={
                "conflict_group_size": len(members),
                "survivor_key": survivor,
                "survivor_degree": degree_map.get(survivor, 0),
                "component_members": list(sorted(members)),
            },
        )
        group_candidates.append(group)

        # Mark pairwise candidates in this chain as suppressed.
        for c in dup_candidates:
            if any(uf.find(ref) == root for ref in c.involved_element_refs):
                c.collision_context["suppressed_by_group"] = group.candidate_id
                c.collision_context["conflict_group_size"] = len(members)

    logger.info(
        "conflict_groups_detected",
        chain_count=len(chain_components),
        group_candidates_created=len(group_candidates),
        suppressed_pairwise=sum(
            1 for c in dup_candidates
            if c.collision_context.get("suppressed_by_group")
        ),
    )

    return group_candidates


# ---------------------------------------------------------------------------
# POST /api/curation/candidates/generate
# ---------------------------------------------------------------------------


@router.post(
    "/candidates/generate",
    response_model=GenerateCandidatesResponse,
    summary="Run all five deterministic detectors and cache candidate results",
    responses={
        409: {
            "model": GenerateCandidatesResponse,
            "description": "Schema not locked — approve Phase 1 before generating candidates",
        },
        503: {
            "model": GenerateCandidatesResponse,
            "description": "Neo4j unavailable — graph read failed",
        },
    },
)
async def generate_candidates(
    request: GenerateCandidatesRequest,
    response: Response,
    reader: GraphReader = Depends(get_graph_reader),
) -> GenerateCandidatesResponse:
    """Run all five detectors for a run and cache candidate results.

    Requires the domain schema to be locked (Phase 1 complete).  Returns 409
    if the schema has not been approved for this run_id.

    Idempotent within the 5-minute cache TTL: a cached result for the same
    schema_version is returned immediately without re-querying Neo4j or
    re-running the detectors.

    All graph reads are delegated to GraphReader (read-only, cache-first,
    5-min TTL).  All detection is delegated to the five detector classes in
    api/services/curation/candidates.py.  No detection logic lives here.

    **Errors**
    - ``409 Conflict`` — no locked schema found for this run_id.
    - ``503 Service Unavailable`` — Neo4j read failed.
    """
    run_id = request.run_id
    stage = request.stage
    logger.info("candidate_generation_requested", run_id=run_id, stage=stage)

    # ------------------------------------------------------------------
    # 0. Validate stage parameter
    # ------------------------------------------------------------------
    if stage is not None and stage not in {1, 2, 3}:
        response.status_code = 422
        return GenerateCandidatesResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="invalid_stage",
                    message=(
                        f"stage must be 1 (duplicates), 2 (canonical), "
                        f"3 (structural), or null (all). Got: {stage}"
                    ),
                )
            ],
        )

    # ------------------------------------------------------------------
    # 1. Verify schema is locked (CLAUDE.md §4.4 fail-closed)
    # ------------------------------------------------------------------
    cache = get_cache_client()
    schema: SchemaVersion | None = await cache.get(
        CacheKey.schema(run_id=run_id),
        model=SchemaVersion,
    )
    if schema is None:
        logger.warning("candidates_schema_not_locked", run_id=run_id)
        response.status_code = 409
        return GenerateCandidatesResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="schema_not_locked",
                    message=(
                        f"No locked schema found for run '{run_id}'. "
                        "Complete Phase 1 (approve the domain schema) before "
                        "generating candidates."
                    ),
                )
            ],
        )

    # ------------------------------------------------------------------
    # 2. Check candidate cache — idempotent re-call within TTL
    # ------------------------------------------------------------------
    dhash = _detection_hash(schema.version_hash, stage=stage)
    cache_key = CacheKey.candidates(run_id=run_id, detection_hash=dhash)

    cached: _CandidateListCache | None = await cache.get(
        cache_key, model=_CandidateListCache
    )
    if cached is not None:
        logger.debug(
            "candidates_cache_hit", run_id=run_id, count=len(cached.candidates)
        )
        return GenerateCandidatesResponse(
            run_id=run_id,
            status="success",
            total_count=len(cached.candidates),
            counts_by_type=_build_type_counts(cached.candidates),
            schema_version=cached.schema_version,
        )

    # ------------------------------------------------------------------
    # 3. Fetch graph data — GraphReader handles its own cache + Neo4j
    # ------------------------------------------------------------------
    try:
        nodes = await reader.get_nodes_by_run(run_id)
        rels = await reader.get_relationships_by_run(run_id)
        orphans = await reader.get_orphans(run_id)
        degrees = await reader.get_node_degrees(run_id)
    except Exception as exc:
        logger.error(
            "candidates_graph_read_failed", run_id=run_id, error=str(exc)
        )
        response.status_code = 503
        return GenerateCandidatesResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="neo4j_unavailable",
                    message=f"Graph read failed for run '{run_id}'.",
                )
            ],
        )

    # ------------------------------------------------------------------
    # 4. Run detectors — stage-aware dispatch
    #    stage=1: duplicates, stage=2: canonical, stage=3: structural,
    #    stage=None: all detectors (backward compatible)
    # ------------------------------------------------------------------
    all_candidates: list[Candidate] = []

    if stage in (1, None):
        all_candidates.extend(
            ExactNodeDuplicateDetector().detect(
                run_id=run_id,
                schema_version=schema.version_hash,
                nodes=nodes,
            )
        )
        all_candidates.extend(
            ExactRelDuplicateDetector().detect(
                run_id=run_id,
                schema_version=schema.version_hash,
                rels=rels,
            )
        )
        all_candidates.extend(
            ProbableDuplicateDetector().detect(
                run_id=run_id,
                schema_version=schema.version_hash,
                nodes=nodes,
                schema=schema,
                rels=rels,
            )
        )

    if stage in (2, None):
        all_candidates.extend(
            CanonicalViolationDetector().detect(
                run_id=run_id,
                schema_version=schema.version_hash,
                rels=rels,
                nodes=nodes,
                schema=schema,
            )
        )

    if stage in (3, None):
        all_candidates.extend(
            StructuralAnomalyDetector().detect(
                run_id=run_id,
                schema_version=schema.version_hash,
                nodes=nodes,
                rels=rels,
                orphans=orphans,
                degrees=degrees,
                schema=schema,
            )
        )

    # ------------------------------------------------------------------
    # 5a. Suppress overlapping candidates: remove orphan_node anomalies
    #     when the same node already appears in a duplicate candidate.
    #     A probable/exact duplicate that gets merged also resolves the
    #     orphan status, so a separate orphan candidate would be redundant
    #     and can cause execution failures (node deleted by merge before
    #     the orphan's proposal executes).
    # ------------------------------------------------------------------
    duplicate_types = {
        CandidateType.exact_node_duplicate,
        CandidateType.probable_duplicate,
    }
    duplicate_refs: set[str] = set()
    for c in all_candidates:
        if c.candidate_type in duplicate_types:
            duplicate_refs.update(c.involved_element_refs)

    filtered: list[Candidate] = []
    suppressed = 0
    for c in all_candidates:
        if (
            c.candidate_type == CandidateType.structural_anomaly
            and c.collision_context.get("dedupe_key") in duplicate_refs
            and c.detection_method == "orphan_node"
        ):
            suppressed += 1
            continue
        filtered.append(c)

    if suppressed:
        logger.info(
            "candidates_overlap_suppressed",
            run_id=run_id,
            suppressed_count=suppressed,
        )

    # ------------------------------------------------------------------
    # 5b. Deduplicate by candidate_id (same graph situation → same identity)
    # ------------------------------------------------------------------
    seen: set[str] = set()
    unique: list[Candidate] = []
    for c in filtered:
        if c.candidate_id not in seen:
            seen.add(c.candidate_id)
            unique.append(c)

    # ------------------------------------------------------------------
    # 5c. Connected-component conflict detection (stage 1 only).
    #     For duplicate-type candidates, build a union-find over
    #     involved_element_refs pairs.  When a connected component has
    #     >2 nodes (a chain like A↔M↔R↔B), create a synthetic group
    #     candidate for the whole chain and suppress individual pairwise
    #     candidates so the agent pipeline merges all nodes atomically.
    # ------------------------------------------------------------------
    if stage in (1, None):
        # Build degree lookup for survivor selection (most-connected wins).
        degree_map: dict[str, int] = {}
        for d in degrees.degrees:
            degree_map[d.dedupe_key] = d.in_degree + d.out_degree

        group_candidates = _annotate_conflict_groups(unique, degree_map)
        unique.extend(group_candidates)

    candidates_out = [_to_candidate_out(c) for c in unique]

    # ------------------------------------------------------------------
    # 6. Cache results with 5-minute TTL (SKILL-D R-D8)
    # ------------------------------------------------------------------
    await cache.set(
        cache_key,
        _CandidateListCache(candidates=candidates_out, schema_version=schema.version_hash),
        ttl=_CANDIDATE_CACHE_TTL,
    )

    logger.info(
        "candidate_generation_complete",
        run_id=run_id,
        total_count=len(unique),
        schema_version=schema.version_hash,
    )

    return GenerateCandidatesResponse(
        run_id=run_id,
        status="success",
        total_count=len(unique),
        counts_by_type=_build_type_counts(candidates_out),
        schema_version=schema.version_hash,
    )


# ---------------------------------------------------------------------------
# Excluded candidate filter helper
# ---------------------------------------------------------------------------


async def _load_excluded_ids(run_id: str) -> set[str]:
    """Load excluded candidate IDs from Redis with graceful degradation.

    Returns an empty set on cache miss or error (SKILL-D R-D9).
    """
    try:
        cache = get_cache_client()
        excluded: _ExcludedCandidatesCache | None = await cache.get(
            CacheKey.excluded_candidates(run_id),
            model=_ExcludedCandidatesCache,
        )
        if excluded is not None:
            return set(excluded.candidate_ids)
    except Exception:
        logger.debug("excluded_candidates_load_failed", run_id=run_id)
    return set()


# ---------------------------------------------------------------------------
# GET /api/curation/candidates/{run_id}
# ---------------------------------------------------------------------------


@router.get(
    "/candidates/{run_id}",
    response_model=ListCandidatesResponse,
    summary="Return cached candidates grouped by type and ordered by severity",
)
async def list_candidates(run_id: str) -> ListCandidatesResponse:
    """Return candidates for a run, grouped by type and ordered by severity.

    Reads from the candidate cache populated by POST /candidates/generate.
    Returns HTTP 200 with ``total_count=0`` and empty ``groups`` if no
    cached candidates exist (generation not yet triggered, or TTL expired).

    Group ordering follows CandidateType enum definition order.
    Within each group, candidates are sorted by severity (critical -> low)
    then by candidate_id for deterministic ordering.

    Always returns HTTP 200.  Callers should trigger generation via
    POST /candidates/generate if the response is empty.
    """
    logger.info("candidates_list_requested", run_id=run_id)

    cache = get_cache_client()

    # Resolve schema version to compute the detection_hash.
    schema: SchemaVersion | None = await cache.get(
        CacheKey.schema(run_id=run_id),
        model=SchemaVersion,
    )
    if schema is None:
        logger.debug("candidates_list_no_schema", run_id=run_id)
        return ListCandidatesResponse(
            run_id=run_id,
            status="success",
            total_count=0,
            groups=[],
            schema_version="",
        )

    # Collect candidates from all stage cache keys (None, 1, 2, 3).
    all_cached_candidates: list[CandidateOut] = []
    for stage_val in (None, 1, 2, 3):
        dhash = _detection_hash(schema.version_hash, stage=stage_val)
        cache_key = CacheKey.candidates(run_id=run_id, detection_hash=dhash)

        cached: _CandidateListCache | None = await cache.get(
            cache_key, model=_CandidateListCache
        )
        if cached is not None and cached.candidates:
            all_cached_candidates.extend(cached.candidates)

    # Deduplicate by candidate_id across stages.
    seen_ids: set[str] = set()
    deduped: list[CandidateOut] = []
    for c in all_cached_candidates:
        if c.candidate_id not in seen_ids:
            seen_ids.add(c.candidate_id)
            deduped.append(c)

    # Filter out excluded candidates.
    excluded_ids = await _load_excluded_ids(run_id)
    if excluded_ids:
        deduped = [c for c in deduped if c.candidate_id not in excluded_ids]

    if not deduped:
        logger.debug("candidates_list_empty", run_id=run_id)
        return ListCandidatesResponse(
            run_id=run_id,
            status="success",
            total_count=0,
            groups=[],
            schema_version=schema.version_hash,
        )

    result = _build_groups(
        run_id=run_id,
        candidates=deduped,
        schema_version=schema.version_hash,
    )

    logger.info(
        "candidates_list_success",
        run_id=run_id,
        total_count=result.total_count,
        group_count=len(result.groups),
    )

    return result


# ---------------------------------------------------------------------------
# POST /api/curation/candidates/generate-scoped
# ---------------------------------------------------------------------------


@router.post(
    "/candidates/generate-scoped",
    response_model=GenerateCandidatesResponse,
    summary="Run detectors scoped to a set of affected node keys (post-merge re-trigger)",
    responses={
        409: {
            "model": GenerateCandidatesResponse,
            "description": "Schema not locked — approve Phase 1 before generating candidates",
        },
        503: {
            "model": GenerateCandidatesResponse,
            "description": "Neo4j unavailable — graph read failed",
        },
    },
)
async def generate_candidates_scoped(
    request: GenerateScopedCandidatesRequest,
    response: Response,
    reader: GraphReader = Depends(get_graph_reader),
) -> GenerateCandidatesResponse:
    """Run candidate detectors scoped to affected node keys.

    Unlike the full ``/candidates/generate`` endpoint, this only evaluates
    candidates involving the specified node keys.  Designed for post-merge
    re-detection or manual re-trigger from the UI.
    """
    run_id = request.run_id
    focus_keys = set(request.affected_node_keys)
    logger.info(
        "scoped_candidate_generation_requested",
        run_id=run_id,
        focus_keys_count=len(focus_keys),
    )

    # Verify schema is locked
    schema_service = get_schema_service()
    schema = await schema_service.get_current(run_id)
    if schema is None:
        logger.warning("candidates_schema_not_locked", run_id=run_id)
        response.status_code = 409
        return GenerateCandidatesResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="schema_not_locked",
                    message=(
                        f"No locked schema found for run '{run_id}'. "
                        "Complete Phase 1 (approve the domain schema) before "
                        "generating candidates."
                    ),
                )
            ],
        )

    try:
        candidates = await run_scoped_detection(
            run_id=run_id,
            schema_version=schema.version_hash,
            focus_keys=focus_keys,
            reader=reader,
            schema=schema,
        )
    except Exception as exc:
        logger.error(
            "scoped_candidates_graph_read_failed",
            run_id=run_id,
            error=str(exc),
        )
        response.status_code = 503
        return GenerateCandidatesResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="neo4j_unavailable",
                    message=f"Graph read failed for run '{run_id}'.",
                )
            ],
        )

    candidates_out = [_to_candidate_out(c) for c in candidates]

    logger.info(
        "scoped_candidate_generation_complete",
        run_id=run_id,
        total_count=len(candidates_out),
        schema_version=schema.version_hash,
    )

    return GenerateCandidatesResponse(
        run_id=run_id,
        status="success",
        total_count=len(candidates_out),
        counts_by_type=_build_type_counts(candidates_out),
        schema_version=schema.version_hash,
    )
