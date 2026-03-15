"""
api/routers/session_models.py — Pydantic models for session persistence endpoints.

SKILL-A R-A1: Every endpoint has request/response models.
SKILL-A R-A5: Models co-located with the route handler module.
SKILL-D R-D5: SessionRecord deliberately excludes passwords and API keys.
"""

from pydantic import BaseModel, ConfigDict

from api.models.responses import BaseResponse


class SessionRecord(BaseModel):
    """Persisted session metadata — NO credentials (SKILL-D R-D5).

    Stored in Redis at ``CacheKey.session(user_hash)`` with no TTL.
    Passwords and API keys are never included — they reside in ``.env`` only.

    Attributes:
        run_id:          Deterministic run identifier (SHA-256).
        timestamp_seed:  ISO 8601 UTC string used to derive run_id.
        phase:           Current phase as integer (Phase enum value).
        schema_version:  Locked schema version string, or None if not yet locked.
        neo4j_uri:       Neo4j Aura connection URI (not a credential).
        neo4j_user:      Neo4j username (not a credential — used as namespace).
    """

    model_config = ConfigDict(strict=True)

    run_id: str
    timestamp_seed: str
    phase: int
    schema_version: str | None = None
    neo4j_uri: str
    neo4j_user: str
    reentry_source: int | None = None


class SaveSessionRequest(BaseModel):
    """Request body for ``POST /api/session/save``."""

    user_hash: str
    record: SessionRecord


class GetSessionResponse(BaseResponse):
    """Response for ``GET /api/session/{user_hash}``."""

    found: bool
    record: SessionRecord | None = None


class SaveSessionResponse(BaseResponse):
    """Response for ``POST /api/session/save``."""

    saved: bool


class DeleteSessionResponse(BaseResponse):
    """Response for ``DELETE /api/session/{user_hash}``."""

    deleted: bool
