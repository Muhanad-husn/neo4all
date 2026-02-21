"""
api/models/responses.py — Base Pydantic response models (SKILL-A R-A2).

BaseResponse is the standard envelope for every API response in this project.
All route response models extend BaseResponse so consumers always receive a
consistent structure: run_id, status, and typed errors.

Usage:
    from api.models.responses import BaseResponse, ErrorDetail

    class MyResponse(BaseResponse):
        data: MyPayload
"""

from typing import Literal

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    """A single structured error descriptor."""

    code: str
    message: str
    field: str | None = None


class BaseResponse(BaseModel):
    """Standard response envelope for all API endpoints (SKILL-A R-A2).

    Fields:
        run_id: Governed Run ID this response is scoped to, or "" for
                system-level endpoints with no active run context.
        status: Aggregate outcome — "success", "error", or "partial".
        errors: Typed error descriptors. Empty list on success.
    """

    run_id: str
    status: Literal["success", "error", "partial"]
    errors: list[ErrorDetail] = []
