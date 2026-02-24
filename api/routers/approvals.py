"""
api/routers/approvals.py — Approval Gate endpoints (SPEC-06 S-06.3).

Implements the human approval step in the governed mutation pipeline.
No graph mutation occurs here — this file only validates proposals and
issues approval tokens that Agent-C later verifies before touching the graph.

Endpoints (mounted at /api/curation):
  POST /proposals/{proposal_id}/approve   — Phase 1 approval.
      Low-risk proposals: issue approval_id immediately, transition proposal
      to "approved", write ApprovalRecord to Redis.
      High-risk proposals (merge, delete): issue a confirmation_token that
      must be presented to /confirm (phase 2) before the approval_id is
      issued.  The proposal state is NOT changed at phase 1.

  POST /proposals/{proposal_id}/confirm   — Phase 2 confirmation (high-risk only).
      Validate confirmation_token, issue approval_id, transition proposal to
      "approved", write ApprovalRecord to Redis.

  POST /proposals/{proposal_id}/reject    — Reject a pending proposal.
  POST /proposals/{proposal_id}/defer     — Defer a pending proposal.

Deterministic ID derivation (CLAUDE.md §5 — no uuid4):
  approval_id        = SHA-256("approval:" + proposal_id + ":" + actor)
  confirmation_token = SHA-256("confirm:"  + proposal_id + ":" + actor)

Both are derived deterministically so that a proposal can only ever produce
one approval_id and one confirmation_token for a given actor.  Because
"approved" is a terminal state, a proposal cannot be re-approved.

Storage contracts:
  - ApprovalRecord stored at CacheKey.approval(approval_id) with no TTL.
    Written here; read by api/agents/execution.py.
  - PendingConfirmation stored at CacheKey.confirmation(token), TTL 3600 s.
    Deleted after phase 2 is completed.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Response
from pydantic import BaseModel, field_validator

from api.agents.execution import ApprovalRecord
from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.models.responses import BaseResponse, ErrorDetail
from api.observability.logger import get_logger
from api.proposals.models import HIGH_RISK_CLASSES, ProposalState
from api.proposals.service import (
    InvalidTransitionError,
    ProposalNotFoundError,
    ProposalService,
    ProposalStorageError,
)

logger = get_logger(__name__)

router = APIRouter(tags=["approvals"])

# Confirmation token TTL: 1 hour.
_CONFIRMATION_TTL: int = 3600


# ---------------------------------------------------------------------------
# Internal cache model — pending phase-1 record for high-risk proposals
# ---------------------------------------------------------------------------


class _PendingConfirmation(BaseModel):
    """Stored in Redis during phase 1 of two-phase approval.

    Holds enough context to issue the full ApprovalRecord when phase 2
    (confirm) arrives.
    """

    proposal_id: str
    run_id: str
    actor: str


# ---------------------------------------------------------------------------
# Deterministic ID derivation helpers
# ---------------------------------------------------------------------------


def _derive_approval_id(proposal_id: str, actor: str) -> str:
    """Deterministic SHA-256 approval token: "approval:{proposal_id}:{actor}"."""
    raw = f"approval:{proposal_id}:{actor}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _derive_confirmation_token(proposal_id: str, actor: str) -> str:
    """Deterministic SHA-256 confirmation token: "confirm:{proposal_id}:{actor}"."""
    raw = f"confirm:{proposal_id}:{actor}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Request / Response models (SKILL-A R-A1, R-A2)
# ---------------------------------------------------------------------------


class ApproveProposalRequest(BaseModel):
    """Request body for POST /proposals/{proposal_id}/approve."""

    run_id: str
    actor: str

    @field_validator("run_id", "actor")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class ApproveProposalResponse(BaseResponse):
    """Response for POST /proposals/{proposal_id}/approve.

    Attributes:
        proposal_id:           The proposal that was approved (or pended).
        approval_id:           Issued approval token (empty when confirmation
                               is required — will be issued after /confirm).
        confirmation_required: True when the proposal is high-risk and the
                               caller must complete /confirm before execution.
        confirmation_token:    Phase-1 token to supply to /confirm (only set
                               when confirmation_required=True).
    """

    proposal_id: str = ""
    approval_id: str = ""
    confirmation_required: bool = False
    confirmation_token: str = ""


class ConfirmProposalRequest(BaseModel):
    """Request body for POST /proposals/{proposal_id}/confirm (phase 2)."""

    run_id: str
    actor: str
    confirmation_token: str

    @field_validator("run_id", "actor", "confirmation_token")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class ConfirmProposalResponse(BaseResponse):
    """Response for POST /proposals/{proposal_id}/confirm.

    Attributes:
        proposal_id: The proposal that was confirmed and approved.
        approval_id: The final approval token to pass to the execute endpoint.
    """

    proposal_id: str = ""
    approval_id: str = ""


class RejectProposalRequest(BaseModel):
    """Request body for POST /proposals/{proposal_id}/reject."""

    run_id: str
    actor: str
    reason: str = ""

    @field_validator("run_id", "actor")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class RejectProposalResponse(BaseResponse):
    """Response for POST /proposals/{proposal_id}/reject."""

    proposal_id: str = ""


class DeferProposalRequest(BaseModel):
    """Request body for POST /proposals/{proposal_id}/defer."""

    run_id: str
    actor: str
    reason: str = ""

    @field_validator("run_id", "actor")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class DeferProposalResponse(BaseResponse):
    """Response for POST /proposals/{proposal_id}/defer."""

    proposal_id: str = ""


# ---------------------------------------------------------------------------
# POST /proposals/{proposal_id}/approve
# ---------------------------------------------------------------------------


@router.post(
    "/proposals/{proposal_id}/approve",
    response_model=ApproveProposalResponse,
    summary="Approve a pending proposal (two-phase for merge/delete)",
    responses={
        404: {
            "model": ApproveProposalResponse,
            "description": "Proposal not found",
        },
        409: {
            "model": ApproveProposalResponse,
            "description": "Proposal is not in a state that permits approval",
        },
        503: {
            "model": ApproveProposalResponse,
            "description": "Proposal storage unavailable",
        },
    },
)
async def approve_proposal(
    proposal_id: str,
    request: ApproveProposalRequest,
    response: Response,
) -> ApproveProposalResponse:
    """Approve a pending proposal and issue an approval token for Agent-C.

    **Low-risk proposals** (canonicalize, normalize, rename, defer):
    The approval_id is issued immediately and the proposal is transitioned to
    "approved".  Pass the approval_id to POST /proposals/{id}/execute.

    **High-risk proposals** (merge, delete):
    Returns ``confirmation_required=True`` and a ``confirmation_token``.  The
    proposal state is not changed.  Call POST /proposals/{id}/confirm with the
    token to complete the approval and receive the approval_id.

    **Errors**
    - ``404 Not Found`` — no proposal with this ID exists.
    - ``409 Conflict`` — proposal is not in "pending" state.
    - ``503 Service Unavailable`` — S3 storage failure during state transition.
    """
    run_id = request.run_id
    actor = request.actor

    logger.info(
        "proposal_approve_requested",
        proposal_id=proposal_id,
        run_id=run_id,
        actor=actor,
    )

    service = ProposalService()
    cache = get_cache_client()

    # Fetch proposal — fail-closed on not found.
    try:
        proposal = await service.get_for_run(run_id, proposal_id)
    except ProposalStorageError as exc:
        logger.error(
            "proposal_fetch_error",
            proposal_id=proposal_id,
            run_id=run_id,
            error=str(exc),
        )
        response.status_code = 503
        return ApproveProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[ErrorDetail(code="storage_error", message=str(exc))],
        )

    if proposal is None:
        logger.warning("proposal_not_found_for_approve", proposal_id=proposal_id, run_id=run_id)
        response.status_code = 404
        return ApproveProposalResponse(
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

    # Validate the transition is permitted.
    if not proposal.can_transition_to(ProposalState.approved):
        logger.warning(
            "proposal_approve_invalid_state",
            proposal_id=proposal_id,
            current_state=str(proposal.state),
        )
        response.status_code = 409
        return ApproveProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="invalid_state_transition",
                    message=(
                        f"Proposal '{proposal_id}' is in state '{proposal.state}' "
                        "and cannot be approved."
                    ),
                )
            ],
        )

    # --- High-risk: two-phase (phase 1) ---
    if proposal.proposal_class in HIGH_RISK_CLASSES:
        token = _derive_confirmation_token(proposal_id, actor)
        pending = _PendingConfirmation(
            proposal_id=proposal_id,
            run_id=run_id,
            actor=actor,
        )
        await cache.set(CacheKey.confirmation(token), pending, ttl=_CONFIRMATION_TTL)

        logger.info(
            "proposal_confirm_pending",
            proposal_id=proposal_id,
            run_id=run_id,
            proposal_class=str(proposal.proposal_class),
        )
        return ApproveProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="success",
            confirmation_required=True,
            confirmation_token=token,
        )

    # --- Low-risk: issue approval_id immediately ---
    approval_id = _derive_approval_id(proposal_id, actor)

    try:
        await service.transition(run_id, proposal_id, ProposalState.approved)
    except (ProposalNotFoundError, InvalidTransitionError) as exc:
        response.status_code = 409
        return ApproveProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[ErrorDetail(code="transition_failed", message=str(exc))],
        )
    except ProposalStorageError as exc:
        response.status_code = 503
        return ApproveProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[ErrorDetail(code="storage_error", message=str(exc))],
        )

    # Write ApprovalRecord for Agent-C (no TTL — immutable for run lifetime).
    approval = ApprovalRecord(
        approval_id=approval_id,
        proposal_id=proposal_id,
        run_id=run_id,
        actor=actor,
        is_confirmed=False,
    )
    await cache.set(CacheKey.approval(approval_id), approval)

    logger.info(
        "proposal_approved",
        proposal_id=proposal_id,
        run_id=run_id,
        actor=actor,
        approval_id=approval_id,
    )

    return ApproveProposalResponse(
        run_id=run_id,
        proposal_id=proposal_id,
        status="success",
        approval_id=approval_id,
        confirmation_required=False,
    )


# ---------------------------------------------------------------------------
# POST /proposals/{proposal_id}/confirm
# ---------------------------------------------------------------------------


@router.post(
    "/proposals/{proposal_id}/confirm",
    response_model=ConfirmProposalResponse,
    summary="Complete two-phase approval by presenting the confirmation token",
    responses={
        400: {
            "model": ConfirmProposalResponse,
            "description": "Invalid or expired confirmation token",
        },
        404: {
            "model": ConfirmProposalResponse,
            "description": "Proposal not found",
        },
        409: {
            "model": ConfirmProposalResponse,
            "description": "Proposal cannot be approved from its current state",
        },
        503: {
            "model": ConfirmProposalResponse,
            "description": "Storage error during state transition",
        },
    },
)
async def confirm_proposal(
    proposal_id: str,
    request: ConfirmProposalRequest,
    response: Response,
) -> ConfirmProposalResponse:
    """Complete the two-phase approval for a high-risk proposal.

    Present the ``confirmation_token`` issued by the /approve phase-1 call.
    On success, the proposal is transitioned to "approved" and the final
    ``approval_id`` is returned for use with the /execute endpoint.

    **Errors**
    - ``400 Bad Request`` — confirmation_token is invalid, expired, or does not
      match the expected proposal_id.
    - ``404 Not Found`` — proposal not found.
    - ``409 Conflict`` — proposal is not in a valid state for approval.
    - ``503 Service Unavailable`` — storage failure during state transition.
    """
    run_id = request.run_id
    actor = request.actor
    token = request.confirmation_token

    logger.info(
        "proposal_confirm_requested",
        proposal_id=proposal_id,
        run_id=run_id,
        actor=actor,
    )

    cache = get_cache_client()

    # Validate the confirmation token.
    pending: _PendingConfirmation | None = await cache.get(
        CacheKey.confirmation(token), model=_PendingConfirmation
    )
    if pending is None:
        logger.warning(
            "proposal_confirm_invalid_token",
            proposal_id=proposal_id,
            run_id=run_id,
        )
        response.status_code = 400
        return ConfirmProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="invalid_confirmation_token",
                    message=(
                        "The confirmation token is invalid or has expired (TTL 1 hour). "
                        "Restart the approval flow via /approve."
                    ),
                )
            ],
        )

    if pending.proposal_id != proposal_id:
        logger.warning(
            "proposal_confirm_token_mismatch",
            expected_proposal_id=proposal_id,
            token_proposal_id=pending.proposal_id,
        )
        response.status_code = 400
        return ConfirmProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="token_proposal_mismatch",
                    message=(
                        f"Confirmation token was issued for proposal "
                        f"'{pending.proposal_id}', not '{proposal_id}'."
                    ),
                )
            ],
        )

    service = ProposalService()

    # Transition proposal → approved.
    try:
        await service.transition(run_id, proposal_id, ProposalState.approved)
    except ProposalNotFoundError:
        response.status_code = 404
        return ConfirmProposalResponse(
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
    except InvalidTransitionError as exc:
        response.status_code = 409
        return ConfirmProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[ErrorDetail(code="transition_failed", message=str(exc))],
        )
    except ProposalStorageError as exc:
        response.status_code = 503
        return ConfirmProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[ErrorDetail(code="storage_error", message=str(exc))],
        )

    # Issue the approval_id and write the ApprovalRecord.
    approval_id = _derive_approval_id(proposal_id, actor)
    approval = ApprovalRecord(
        approval_id=approval_id,
        proposal_id=proposal_id,
        run_id=run_id,
        actor=actor,
        is_confirmed=True,
    )
    await cache.set(CacheKey.approval(approval_id), approval)

    # Clean up the one-time confirmation token.
    await cache.delete(CacheKey.confirmation(token))

    logger.info(
        "proposal_confirmed_and_approved",
        proposal_id=proposal_id,
        run_id=run_id,
        actor=actor,
        approval_id=approval_id,
    )

    return ConfirmProposalResponse(
        run_id=run_id,
        proposal_id=proposal_id,
        status="success",
        approval_id=approval_id,
    )


# ---------------------------------------------------------------------------
# POST /proposals/{proposal_id}/reject
# ---------------------------------------------------------------------------


@router.post(
    "/proposals/{proposal_id}/reject",
    response_model=RejectProposalResponse,
    summary="Reject a pending proposal",
    responses={
        404: {
            "model": RejectProposalResponse,
            "description": "Proposal not found",
        },
        409: {
            "model": RejectProposalResponse,
            "description": "Proposal cannot be rejected from its current state",
        },
        503: {
            "model": RejectProposalResponse,
            "description": "Storage error during state transition",
        },
    },
)
async def reject_proposal(
    proposal_id: str,
    request: RejectProposalRequest,
    response: Response,
) -> RejectProposalResponse:
    """Reject a pending proposal.

    Transitions the proposal to the terminal "rejected" state.  Rejected
    proposals cannot be re-opened.

    **Errors**
    - ``404 Not Found`` — proposal not found.
    - ``409 Conflict`` — proposal is already terminal (approved or rejected).
    - ``503 Service Unavailable`` — storage failure.
    """
    run_id = request.run_id

    logger.info(
        "proposal_reject_requested",
        proposal_id=proposal_id,
        run_id=run_id,
        actor=request.actor,
    )

    service = ProposalService()

    try:
        await service.transition(run_id, proposal_id, ProposalState.rejected)
    except ProposalNotFoundError:
        response.status_code = 404
        return RejectProposalResponse(
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
    except InvalidTransitionError as exc:
        response.status_code = 409
        return RejectProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[ErrorDetail(code="transition_failed", message=str(exc))],
        )
    except ProposalStorageError as exc:
        response.status_code = 503
        return RejectProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[ErrorDetail(code="storage_error", message=str(exc))],
        )

    logger.info(
        "proposal_rejected",
        proposal_id=proposal_id,
        run_id=run_id,
        actor=request.actor,
    )

    return RejectProposalResponse(
        run_id=run_id,
        proposal_id=proposal_id,
        status="success",
    )


# ---------------------------------------------------------------------------
# POST /proposals/{proposal_id}/defer
# ---------------------------------------------------------------------------


@router.post(
    "/proposals/{proposal_id}/defer",
    response_model=DeferProposalResponse,
    summary="Defer a pending proposal for later review",
    responses={
        404: {
            "model": DeferProposalResponse,
            "description": "Proposal not found",
        },
        409: {
            "model": DeferProposalResponse,
            "description": "Proposal cannot be deferred from its current state",
        },
        503: {
            "model": DeferProposalResponse,
            "description": "Storage error during state transition",
        },
    },
)
async def defer_proposal(
    proposal_id: str,
    request: DeferProposalRequest,
    response: Response,
) -> DeferProposalResponse:
    """Defer a pending proposal — acknowledge it but take no action now.

    Deferred proposals can be re-opened (transitioned back to "pending") by
    the ProposalService.  Use this when the candidate needs more investigation.

    **Errors**
    - ``404 Not Found`` — proposal not found.
    - ``409 Conflict`` — proposal is in a state that cannot be deferred (e.g.
      already approved or rejected).
    - ``503 Service Unavailable`` — storage failure.
    """
    run_id = request.run_id

    logger.info(
        "proposal_defer_requested",
        proposal_id=proposal_id,
        run_id=run_id,
        actor=request.actor,
    )

    service = ProposalService()

    try:
        await service.transition(run_id, proposal_id, ProposalState.deferred)
    except ProposalNotFoundError:
        response.status_code = 404
        return DeferProposalResponse(
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
    except InvalidTransitionError as exc:
        response.status_code = 409
        return DeferProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[ErrorDetail(code="transition_failed", message=str(exc))],
        )
    except ProposalStorageError as exc:
        response.status_code = 503
        return DeferProposalResponse(
            run_id=run_id,
            proposal_id=proposal_id,
            status="error",
            errors=[ErrorDetail(code="storage_error", message=str(exc))],
        )

    logger.info(
        "proposal_deferred",
        proposal_id=proposal_id,
        run_id=run_id,
        actor=request.actor,
    )

    return DeferProposalResponse(
        run_id=run_id,
        proposal_id=proposal_id,
        status="success",
    )
