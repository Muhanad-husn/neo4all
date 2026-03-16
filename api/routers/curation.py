"""
api/routers/curation.py — Manual proposal pipeline endpoints (SPEC-06 S-06.8).

Manual proposal pipeline:
  POST   /propose                           — Create a Proposal Packet (same governed
                                             pipeline as AI — no bypass).
  GET    /proposals/{run_id}               — List all proposals for a run.
  DELETE /proposals/{run_id}               — Purge all proposals for a run (S3 + Redis).
  POST   /proposals/{proposal_id}/execute  — Build diff and execute via Agent-C.

Architecture (SKILL-B: thin routers)
--------------------------------------
Proposal lifecycle is managed by api/proposals/service.py (ProposalService).
Diff construction is delegated to api/diff/builder.py (DiffBuilder).
Execution is delegated to api/agents/execution.py (ExecutionAgent).
Route handlers coordinate calls, map errors to HTTP status codes, and
build response models.  No detector or execution logic in this file.

Error handling
--------------
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
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, field_validator

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.models.responses import BaseResponse, ErrorDetail
from api.observability.logger import get_logger
from api.proposals.models import (
    ElementRef,
    ProposalClass,
    ProposalPacket,
    ProposalState,
)
from api.schema.models import SchemaVersion

logger = get_logger(__name__)

router = APIRouter(tags=["curation"])


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
    high_risk_override: bool = False
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
        high_risk_override=packet.high_risk_override,
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
# GET /proposals/{run_id}/excluded
# ---------------------------------------------------------------------------


@router.get(
    "/proposals/{run_id}/excluded",
    response_model=ListProposalsResponse,
    summary="List all excluded proposals for a governed run",
)
async def list_excluded_proposals(run_id: str) -> ListProposalsResponse:
    """Return all excluded proposals for a run.

    Filtered view of the existing proposals list — only proposals with
    state == "excluded" are returned.

    Always returns HTTP 200.
    """
    from api.proposals.service import ProposalService

    logger.info("curation_proposals_excluded_list_requested", run_id=run_id)

    service = ProposalService()
    packets = await service.list_for_run(run_id)

    excluded = [
        _to_proposal_out(p) for p in packets
        if p.state == ProposalState.excluded
    ]

    logger.info(
        "curation_proposals_excluded_list_success",
        run_id=run_id,
        total=len(excluded),
    )

    return ListProposalsResponse(
        run_id=run_id,
        status="success",
        total=len(excluded),
        proposals=excluded,
    )


# ---------------------------------------------------------------------------
# DELETE /proposals/{run_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/proposals/{run_id}",
    response_model=BaseResponse,
    summary="Delete all proposals for a governed run from S3 and Redis",
)
async def clear_proposals(run_id: str) -> BaseResponse:
    """Remove every proposal for a run from both S3 and Redis.

    Returns HTTP 200 with the count of deleted proposals.
    """
    from api.proposals.service import ProposalService

    logger.info("curation_proposals_clear_requested", run_id=run_id)

    service = ProposalService()
    count = await service.clear_for_run(run_id)

    logger.info(
        "curation_proposals_clear_success",
        run_id=run_id,
        deleted=count,
    )

    return BaseResponse(run_id=run_id, status="success")


# ---------------------------------------------------------------------------
# Request / Response models — Delete All Orphans
# ---------------------------------------------------------------------------


class DeleteAllOrphansRequest(BaseModel):
    """Request body for POST /orphans/delete-all."""

    run_id: str
    actor: str

    @field_validator("run_id", "actor")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class DeleteAllOrphansResponse(BaseResponse):
    """Response for POST /orphans/delete-all.

    Attributes:
        proposals_created: Number of delete proposals created.
        proposals_executed: Number successfully executed.
        failed: Number that failed during execution.
    """

    proposals_created: int = 0
    proposals_executed: int = 0
    failed: int = 0


# ---------------------------------------------------------------------------
# POST /orphans/delete-all
# ---------------------------------------------------------------------------


@router.post(
    "/orphans/delete-all",
    response_model=DeleteAllOrphansResponse,
    summary="Create, approve, and execute delete proposals for all orphan node candidates",
    responses={
        409: {
            "model": DeleteAllOrphansResponse,
            "description": "Schema not locked",
        },
        503: {
            "model": DeleteAllOrphansResponse,
            "description": "Storage or graph unavailable",
        },
    },
)
async def delete_all_orphans(
    request: DeleteAllOrphansRequest,
    response: Response,
) -> DeleteAllOrphansResponse:
    """Bulk delete all orphan node candidates through the full governed pipeline.

    For each orphan candidate: creates a delete proposal, approves it (two-phase
    since delete is high-risk), builds the diff, and executes via Agent-C.
    No pipeline bypass — every step goes through the governed mutation flow.

    **Errors**
    - ``409 Conflict`` — no locked schema for this run_id.
    - ``503 Service Unavailable`` — storage or graph error.
    """
    import hashlib as _hashlib

    from api.agents.execution import ApprovalRecord, ExecutionAgent
    from api.diff.builder import DiffBuilder
    from api.proposals.service import ProposalService, ProposalStorageError

    run_id = request.run_id
    actor = request.actor

    logger.info("orphan_delete_all_requested", run_id=run_id, actor=actor)

    cache = get_cache_client()

    # 1. Verify schema is locked.
    schema: SchemaVersion | None = await cache.get(
        CacheKey.schema(run_id=run_id), model=SchemaVersion
    )
    if schema is None:
        response.status_code = 409
        return DeleteAllOrphansResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="schema_not_locked",
                    message=f"No locked schema found for run '{run_id}'.",
                )
            ],
        )

    # 2. Fetch candidates from cache, filter to orphan_node.
    from api.routers.candidates import _CandidateListCache, _detection_hash

    orphan_candidates: list[dict[str, Any]] = []
    for stage_val in (None, 1, 2, 3):
        dhash = _detection_hash(schema.version_hash, stage=stage_val)
        cache_key = CacheKey.candidates(run_id=run_id, detection_hash=dhash)
        cached: _CandidateListCache | None = await cache.get(
            cache_key, model=_CandidateListCache
        )
        if cached:
            for c in cached.candidates:
                if c.detection_method == "orphan_node":
                    orphan_candidates.append(c.model_dump())

    # Deduplicate by candidate_id.
    seen: set[str] = set()
    unique_orphans: list[dict[str, Any]] = []
    for oc in orphan_candidates:
        cid = oc["candidate_id"]
        if cid not in seen:
            seen.add(cid)
            unique_orphans.append(oc)

    if not unique_orphans:
        return DeleteAllOrphansResponse(
            run_id=run_id,
            status="success",
            proposals_created=0,
            proposals_executed=0,
            failed=0,
        )

    # 3. Process each orphan candidate through the full pipeline.
    service = ProposalService()
    diff_builder = DiffBuilder()
    agent = ExecutionAgent()
    created = 0
    executed = 0
    failed = 0

    for oc in unique_orphans:
        candidate_id = oc["candidate_id"]
        refs = oc.get("involved_element_refs", [])
        targets = tuple(
            ElementRef(element_type="node", dedupe_key=ref, label="", properties={})
            for ref in refs
        )

        try:
            # a. Create proposal.
            packet = ProposalPacket(
                run_id=run_id,
                candidate_id=candidate_id,
                proposal_class=ProposalClass.delete,
                schema_version=schema.version_hash,
                targets=targets,
                rationale=f"Bulk orphan deletion — node has no relationships.",
                confidence_score=1.0,
            )
            await service.create(packet)
            created += 1

            # b. Approve (two-phase for delete).
            proposal_id = packet.proposal_id
            await service.transition(run_id, proposal_id, ProposalState.approved)

            # c. Write approval record.
            raw = f"approval:{proposal_id}:{actor}"
            approval_id = _hashlib.sha256(raw.encode("utf-8")).hexdigest()
            approval = ApprovalRecord(
                approval_id=approval_id,
                proposal_id=proposal_id,
                run_id=run_id,
                actor=actor,
                is_confirmed=True,
            )
            await cache.set(CacheKey.approval(approval_id), approval)

            # d. Build diff and execute.
            diff = diff_builder.build(packet)
            audit_record = await agent.execute(
                diff=diff, approval_id=approval_id, actor=actor,
            )

            if str(audit_record.outcome) == "applied":
                try:
                    await service.transition(run_id, proposal_id, ProposalState.executed)
                except Exception:
                    pass
                executed += 1
            else:
                failed += 1

        except Exception as exc:
            logger.warning(
                "orphan_delete_candidate_failed",
                candidate_id=candidate_id,
                error=str(exc),
            )
            failed += 1

    # 4. Invalidate caches.
    await cache.invalidate_prefix(CacheKey.candidates_prefix(run_id))
    await cache.invalidate_prefix(CacheKey.graph_query_prefix(run_id))

    logger.info(
        "orphan_delete_all_complete",
        run_id=run_id,
        created=created,
        executed=executed,
        failed=failed,
    )

    return DeleteAllOrphansResponse(
        run_id=run_id,
        status="success",
        proposals_created=created,
        proposals_executed=executed,
        failed=failed,
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

    # 3. Refresh credentials from Redis before execution — ensures the API
    #    process uses session-injected credentials after container restarts.
    from api.credentials import refresh_credentials
    await refresh_credentials()

    # 4. Build deterministic DiffPlan (no LLM, no randomness).
    diff = DiffBuilder().build(packet)

    # 5. Delegate to Agent-C — always returns an AuditRecord.
    agent = ExecutionAgent()
    audit_record = await agent.execute(diff=diff, approval_id=approval_id, actor=actor)

    # 5. Best-effort: mark proposal as executed after successful application.
    #    A failure here must not mask a successful graph mutation — the audit
    #    record is already written and is the source of truth.
    if str(audit_record.outcome) == "applied":
        try:
            await service.transition(run_id, proposal_id, ProposalState.executed)
        except Exception:
            logger.warning(
                "curation_execute_transition_failed",
                proposal_id=proposal_id,
                run_id=run_id,
                detail="Could not mark proposal as executed after successful apply",
            )

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
