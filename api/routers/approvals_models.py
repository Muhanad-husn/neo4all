"""
api/routers/approvals_models.py — Request/response models for the Approval Gate endpoints.

These Pydantic models define the API contracts for the six approval-gate
endpoints (approve, confirm, reject, defer, exclude, restore).  Extracted
from approvals.py to keep the router module focused on HTTP handling.

All response models extend BaseResponse (SKILL-A R-A2).
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from api.models.responses import BaseResponse


# ---------------------------------------------------------------------------
# Approve
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
                               is required -- will be issued after /confirm).
        confirmation_required: True when the proposal is high-risk and the
                               caller must complete /confirm before execution.
        confirmation_token:    Phase-1 token to supply to /confirm (only set
                               when confirmation_required=True).
    """

    proposal_id: str = ""
    approval_id: str = ""
    confirmation_required: bool = False
    confirmation_token: str = ""


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Defer
# ---------------------------------------------------------------------------


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
# Exclude
# ---------------------------------------------------------------------------


class ExcludeProposalRequest(BaseModel):
    """Request body for POST /proposals/{proposal_id}/exclude."""

    run_id: str
    actor: str
    reason: str = ""

    @field_validator("run_id", "actor")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class ExcludeProposalResponse(BaseResponse):
    """Response for POST /proposals/{proposal_id}/exclude."""

    proposal_id: str = ""


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class RestoreProposalRequest(BaseModel):
    """Request body for POST /proposals/{proposal_id}/restore."""

    run_id: str
    actor: str

    @field_validator("run_id", "actor")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class RestoreProposalResponse(BaseResponse):
    """Response for POST /proposals/{proposal_id}/restore."""

    proposal_id: str = ""
