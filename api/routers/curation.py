"""
api/routers/curation.py — Curation endpoints (SPEC-05 S-05.4, SPEC-06 S-06.8).

Candidate generation lifecycle (SPEC-05):
  POST /candidates/generate  — Run all five deterministic detectors, cache results.
  GET  /candidates/{run_id}  — Return cached candidates grouped by type.

Manual proposal pipeline (SPEC-06):
  POST /propose                           — Create a Proposal Packet (same governed
                                           pipeline as AI — no bypass).
  GET  /proposals/{run_id}               — List all proposals for a run.
  POST /proposals/{proposal_id}/execute  — Build diff and execute via Agent-C.

Architecture (SKILL-B: thin routers)
--------------------------------------
All detection logic lives in api/services/curation/candidates.py.
All graph reads are delegated to api/graph/reader.py (GraphReader).
Proposal lifecycle is managed by api/proposals/service.py (ProposalService).
Diff construction is delegated to api/diff/builder.py (DiffBuilder).
Execution is delegated to api/agents/execution.py (ExecutionAgent).
Route handlers coordinate calls, map errors to HTTP status codes, and
build response models.  No detector or execution logic in this file.

Error handling
--------------
POST /candidates/generate:
  409 — no locked schema for this run_id (approve Phase 1 first).
  503 — Neo4j unavailable; graph read failed.
GET /candidates/{run_id}:
  Always HTTP 200.  Returns empty groups if generation has not yet been
  triggered or the 5-minute cache TTL has expired.
POST /propose:
  409 — schema not locked for this run_id.
  422 — invalid proposal fields (Pydantic validation failure).
  503 — S3 storage error.
GET /proposals/{run_id}:
  Always HTTP 200.  Returns empty list if no proposals exist.
POST /proposals/{proposal_id}/execute:
  404 — proposal not found.
  409 — proposal not in "approved" state.
  200 — returns AuditRecord summary (outcome may be "failed" or "rejected").

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
from pydantic import BaseModel, field_validator

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.graph.reader import GraphReader, get_graph_reader
from api.models.candidate import Candidate, CandidateType
from api.models.responses import BaseResponse, ErrorDetail
from api.observability.logger import get_logger
from api.proposals.models import (
    ElementRef,
    ProposalClass,
    ProposalPacket,
    ProposalState,
)
from api.schema.models import SchemaVersion
from api.services.curation.candidates import (
    CanonicalViolationDetector,
    ExactNodeDuplicateDetector,
    ExactRelDuplicateDetector,
    ProbableDuplicateDetector,
    StructuralAnomalyDetector,
)

logger = get_logger(__name__)

router = APIRouter(tags=["curation"])

# 5-minute TTL for candidate results (SKILL-D R-D8)
_CANDIDATE_CACHE_TTL: int = 300

# Severity sort order: critical first.
_SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# ---------------------------------------------------------------------------
# Cache key helper
# ---------------------------------------------------------------------------


def _detection_hash(schema_version: str) -> str:
    """Deterministic 32-char hash of detection parameters.

    Encodes schema_version + "all_detectors_v1" sentinel so that:
    - The same schema always produces the same hash (stable across restarts).
    - A future change to detector set or schema would produce a different key.

    Returns the first 32 hex chars of the SHA-256 digest.
    """
    payload = f"{schema_version}:all_detectors_v1"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Request / Response models (SKILL-A R-A1, R-A2)
# ---------------------------------------------------------------------------


class GenerateCandidatesRequest(BaseModel):
    """Request body for POST /api/curation/candidates/generate."""

    run_id: str


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
      (critical → high → medium → low), then by candidate_id for
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
    logger.info("candidate_generation_requested", run_id=run_id)

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
    dhash = _detection_hash(schema.version_hash)
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
    # 4. Run all five detectors (zero-LLM, deterministic)
    # ------------------------------------------------------------------
    all_candidates: list[Candidate] = []

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
    all_candidates.extend(
        CanonicalViolationDetector().detect(
            run_id=run_id,
            schema_version=schema.version_hash,
            rels=rels,
            nodes=nodes,
            schema=schema,
        )
    )
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
    # 5. Deduplicate by candidate_id (same graph situation → same identity)
    # ------------------------------------------------------------------
    seen: set[str] = set()
    unique: list[Candidate] = []
    for c in all_candidates:
        if c.candidate_id not in seen:
            seen.add(c.candidate_id)
            unique.append(c)

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
    Within each group, candidates are sorted by severity (critical → low)
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

    dhash = _detection_hash(schema.version_hash)
    cache_key = CacheKey.candidates(run_id=run_id, detection_hash=dhash)

    cached: _CandidateListCache | None = await cache.get(
        cache_key, model=_CandidateListCache
    )
    if cached is None or not cached.candidates:
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
        candidates=cached.candidates,
        schema_version=cached.schema_version,
    )

    logger.info(
        "candidates_list_success",
        run_id=run_id,
        total_count=result.total_count,
        group_count=len(result.groups),
    )

    return result


# ===========================================================================
# SPEC-06 S-06.8: Manual proposal pipeline
# ===========================================================================

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ElementRefIn(BaseModel):
    """Request-side representation of a graph element targeted by a proposal.

    Mirrors ElementRef but without frozen constraints — acts as the deserialized
    input from the human operator before conversion to the governed model.
    """

    element_type: str  # "node" | "relationship"
    dedupe_key: str
    label: str = ""
    properties: dict[str, Any] = {}

    @field_validator("element_type")
    @classmethod
    def _valid_element_type(cls, v: str) -> str:
        allowed = {"node", "relationship"}
        if v not in allowed:
            raise ValueError(f"element_type must be one of {allowed!r}")
        return v

    @field_validator("dedupe_key")
    @classmethod
    def _non_empty_dedupe_key(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("dedupe_key must be non-empty")
        return v


class ProposeRequest(BaseModel):
    """Request body for POST /propose.

    The schema_version is resolved automatically from the run's locked schema
    — callers do not supply it directly.

    rationale is required and must be non-empty (CLAUDE.md §6 governance field).
    confidence_score defaults to 1.0 for human operators.
    """

    run_id: str
    candidate_id: str
    proposal_class: ProposalClass
    evidence_chunk_ids: list[str] = []
    evidence_doc_ids: list[str] = []
    targets: list[ElementRefIn] = []
    rule_ids: list[str] = []
    rationale: str
    confidence_score: float = 1.0

    @field_validator("run_id", "candidate_id", "rationale")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id_length(cls, v: str) -> str:
        if len(v) != 64:
            raise ValueError(
                f"candidate_id must be a 64-character SHA-256 hex digest, "
                f"got {len(v)} chars"
            )
        return v


class ProposeResponse(BaseResponse):
    """Response for POST /propose.

    Attributes:
        proposal_id: Deterministic SHA-256 of the new ProposalPacket.
        state:       Initial state ("pending").
    """

    proposal_id: str = ""
    state: str = ""


class ProposalOut(BaseModel):
    """Serialisable representation of a ProposalPacket for API responses.

    Converts frozen tuple fields to lists and enum fields to strings so the
    response is fully JSON-native (SKILL-A R-A3).
    """

    proposal_id: str
    run_id: str
    candidate_id: str
    proposal_class: str
    schema_version: str
    evidence_chunk_ids: list[str]
    evidence_doc_ids: list[str]
    targets: list[dict[str, Any]]
    rule_ids: list[str]
    rationale: str
    confidence_score: float
    state: str


class ListProposalsResponse(BaseResponse):
    """Response for GET /proposals/{run_id}.

    Attributes:
        total:     Total proposals for this run.
        proposals: All proposals in creation order.
    """

    total: int = 0
    proposals: list[ProposalOut] = []


class ExecuteProposalRequest(BaseModel):
    """Request body for POST /proposals/{proposal_id}/execute.

    Attributes:
        run_id:      Governed run the proposal belongs to.
        approval_id: Token issued by the Approval Gate for this proposal.
        actor:       Identity of the human operator initiating execution.
    """

    run_id: str
    approval_id: str
    actor: str

    @field_validator("run_id", "approval_id", "actor")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class ExecuteProposalResponse(BaseResponse):
    """Response for POST /proposals/{proposal_id}/execute.

    Returns the AuditRecord summary.  The outcome field indicates whether
    execution succeeded; check error_detail for failure reasons.

    Attributes:
        proposal_id:   The proposal that was executed.
        diff_id:       SHA-256 of the DiffPlan that was applied.
        approval_id:   The approval token used to authorise execution.
        outcome:       "applied", "failed", or "rejected".
        steps_applied: Number of DiffSteps successfully applied.
        error_detail:  Structured failure payload (empty on success).
    """

    proposal_id: str = ""
    diff_id: str = ""
    approval_id: str = ""
    outcome: str = ""
    steps_applied: int = 0
    error_detail: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_proposal_out(packet: ProposalPacket) -> ProposalOut:
    """Convert a frozen ProposalPacket to its API-safe ProposalOut representation."""
    return ProposalOut(
        proposal_id=packet.proposal_id,
        run_id=packet.run_id,
        candidate_id=packet.candidate_id,
        proposal_class=str(packet.proposal_class),
        schema_version=packet.schema_version,
        evidence_chunk_ids=list(packet.evidence_chunk_ids),
        evidence_doc_ids=list(packet.evidence_doc_ids),
        targets=[
            {
                "element_type": t.element_type,
                "dedupe_key": t.dedupe_key,
                "label": t.label,
                "properties": dict(t.properties),
            }
            for t in packet.targets
        ],
        rule_ids=list(packet.rule_ids),
        rationale=packet.rationale,
        confidence_score=packet.confidence_score,
        state=str(packet.state),
    )


# ---------------------------------------------------------------------------
# POST /propose
# ---------------------------------------------------------------------------


@router.post(
    "/propose",
    response_model=ProposeResponse,
    summary="Create a manual Proposal Packet (same governed pipeline as AI)",
    responses={
        409: {
            "model": ProposeResponse,
            "description": "Schema not locked — approve Phase 1 first",
        },
        503: {
            "model": ProposeResponse,
            "description": "Proposal storage unavailable",
        },
    },
)
async def propose(
    request: ProposeRequest,
    response: Response,
) -> ProposeResponse:
    """Create a Proposal Packet for a candidate.

    Human proposals follow the same governed pipeline as AI proposals (Agent-P):
    the packet is stored via ProposalService and must pass through the Approval
    Gate before any graph mutation occurs.  There is no bypass.

    The schema_version is resolved from the run's locked schema — the schema
    must be approved (Phase 1 complete) before proposals can be created.

    **Errors**
    - ``409 Conflict`` — no locked schema found for this run_id.
    - ``503 Service Unavailable`` — S3 storage failure.
    """
    from api.proposals.service import ProposalService, ProposalStorageError

    run_id = request.run_id
    logger.info(
        "curation_propose_requested",
        run_id=run_id,
        candidate_id=request.candidate_id,
        proposal_class=str(request.proposal_class),
    )

    cache = get_cache_client()

    # 1. Resolve locked schema — fail-closed if absent.
    schema: SchemaVersion | None = await cache.get(
        CacheKey.schema(run_id=run_id), model=SchemaVersion
    )
    if schema is None:
        logger.warning("curation_propose_schema_not_locked", run_id=run_id)
        response.status_code = 409
        return ProposeResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="schema_not_locked",
                    message=(
                        f"No locked schema found for run '{run_id}'. "
                        "Complete Phase 1 (approve the domain schema) first."
                    ),
                )
            ],
        )

    # 2. Convert request targets to typed ElementRef instances.
    targets = tuple(
        ElementRef(
            element_type=t.element_type,
            dedupe_key=t.dedupe_key,
            label=t.label,
            properties=dict(t.properties),
        )
        for t in request.targets
    )

    # 3. Construct the frozen ProposalPacket.
    packet = ProposalPacket(
        run_id=run_id,
        candidate_id=request.candidate_id,
        proposal_class=request.proposal_class,
        schema_version=schema.version_hash,
        evidence_chunk_ids=tuple(request.evidence_chunk_ids),
        evidence_doc_ids=tuple(request.evidence_doc_ids),
        targets=targets,
        rule_ids=tuple(request.rule_ids),
        rationale=request.rationale,
        confidence_score=request.confidence_score,
    )

    # 4. Persist via ProposalService (idempotent on same proposal_id).
    service = ProposalService()
    try:
        stored = await service.create(packet)
    except ProposalStorageError as exc:
        logger.error(
            "curation_propose_storage_error",
            run_id=run_id,
            proposal_id=packet.proposal_id,
            error=str(exc),
        )
        response.status_code = 503
        return ProposeResponse(
            run_id=run_id,
            status="error",
            errors=[ErrorDetail(code="storage_error", message=str(exc))],
        )

    logger.info(
        "curation_propose_success",
        run_id=run_id,
        proposal_id=stored.proposal_id,
        proposal_class=str(stored.proposal_class),
    )

    return ProposeResponse(
        run_id=run_id,
        status="success",
        proposal_id=stored.proposal_id,
        state=str(stored.state),
    )


# ---------------------------------------------------------------------------
# GET /proposals/{run_id}
# ---------------------------------------------------------------------------


@router.get(
    "/proposals/{run_id}",
    response_model=ListProposalsResponse,
    summary="List all proposals for a governed run in creation order",
)
async def list_proposals(run_id: str) -> ListProposalsResponse:
    """Return all proposals for a run, in creation order.

    Reads from the per-run proposal index maintained by ProposalService.
    Returns HTTP 200 with ``total=0`` and empty ``proposals`` if no proposals
    have been created yet.

    Always returns HTTP 200.
    """
    from api.proposals.service import ProposalService

    logger.info("curation_proposals_list_requested", run_id=run_id)

    service = ProposalService()
    packets = await service.list_for_run(run_id)

    proposals = [_to_proposal_out(p) for p in packets]

    logger.info(
        "curation_proposals_list_success",
        run_id=run_id,
        total=len(proposals),
    )

    return ListProposalsResponse(
        run_id=run_id,
        status="success",
        total=len(proposals),
        proposals=proposals,
    )


# ---------------------------------------------------------------------------
# POST /proposals/{proposal_id}/execute
# ---------------------------------------------------------------------------


@router.post(
    "/proposals/{proposal_id}/execute",
    response_model=ExecuteProposalResponse,
    summary="Build a deterministic diff and execute an approved proposal via Agent-C",
    responses={
        404: {
            "model": ExecuteProposalResponse,
            "description": "Proposal not found",
        },
        409: {
            "model": ExecuteProposalResponse,
            "description": "Proposal is not in 'approved' state",
        },
        503: {
            "model": ExecuteProposalResponse,
            "description": "Proposal storage unavailable",
        },
    },
)
async def execute_proposal(
    proposal_id: str,
    request: ExecuteProposalRequest,
    response: Response,
) -> ExecuteProposalResponse:
    """Execute an approved proposal through the full governed pipeline.

    Fetches the proposal, builds a deterministic DiffPlan, then delegates
    to Agent-C (ExecutionAgent) for pre-execution validation, step application,
    post-apply invariant checks, cache invalidation, and audit logging.

    Pre-execution validation is enforced by Agent-C (fail-closed):
    - approval_id must exist in Redis.
    - approval_id must refer to this proposal_id.
    - Proposal must be in "approved" state.
    - DiffPlan schema_version must match current locked schema.

    The response always includes an ``outcome`` field:
    - ``applied``  — all steps succeeded and graph was mutated.
    - ``failed``   — a step or invariant check failed; graph may be partially
                     mutated (check error_detail).
    - ``rejected`` — pre-execution validation blocked execution; graph not touched.

    **Errors**
    - ``404 Not Found`` — proposal_id not found in storage.
    - ``409 Conflict`` — proposal exists but is not in "approved" state.
    - ``503 Service Unavailable`` — proposal storage error.
    """
    from api.agents.execution import ExecutionAgent
    from api.diff.builder import DiffBuilder
    from api.proposals.service import ProposalService, ProposalStorageError

    run_id = request.run_id
    approval_id = request.approval_id
    actor = request.actor

    logger.info(
        "curation_execute_requested",
        proposal_id=proposal_id,
        run_id=run_id,
        actor=actor,
    )

    service = ProposalService()

    # 1. Fetch the proposal — fail-closed on not found.
    try:
        packet = await service.get_for_run(run_id, proposal_id)
    except ProposalStorageError as exc:
        response.status_code = 503
        return ExecuteProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[ErrorDetail(code="storage_error", message=str(exc))],
        )

    if packet is None:
        logger.warning(
            "curation_execute_proposal_not_found",
            proposal_id=proposal_id,
            run_id=run_id,
        )
        response.status_code = 404
        return ExecuteProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="proposal_not_found",
                    message=f"Proposal '{proposal_id}' not found in run '{run_id}'.",
                )
            ],
        )

    # 2. Guard: proposal must be approved before we build the diff.
    #    Agent-C enforces this again, but early rejection gives a cleaner 409.
    if packet.state != ProposalState.approved:
        logger.warning(
            "curation_execute_not_approved",
            proposal_id=proposal_id,
            state=str(packet.state),
        )
        response.status_code = 409
        return ExecuteProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="proposal_not_approved",
                    message=(
                        f"Proposal '{proposal_id}' is in state '{packet.state}'. "
                        "Approve it via POST /proposals/{id}/approve first."
                    ),
                )
            ],
        )

    # 3. Build deterministic DiffPlan (no LLM, no randomness).
    diff = DiffBuilder().build(packet)

    # 4. Delegate to Agent-C — always returns an AuditRecord.
    agent = ExecutionAgent()
    audit_record = await agent.execute(diff=diff, approval_id=approval_id, actor=actor)

    logger.info(
        "curation_execute_complete",
        proposal_id=proposal_id,
        run_id=run_id,
        diff_id=audit_record.diff_id,
        outcome=str(audit_record.outcome),
        steps_applied=audit_record.steps_applied,
    )

    return ExecuteProposalResponse(
        run_id=run_id,
        proposal_id=proposal_id,
        status="success",
        approval_id=audit_record.approval_id,
        diff_id=audit_record.diff_id,
        outcome=str(audit_record.outcome),
        steps_applied=audit_record.steps_applied,
        error_detail=dict(audit_record.error_detail),
    )
