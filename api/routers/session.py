"""
api/routers/session.py — Session persistence endpoints.

POST /save           — Persist SessionRecord to Redis (fire-and-forget from UI).
GET  /{user_hash}    — Retrieve SessionRecord or found=False if absent.
DELETE /{user_hash}  — Clear persisted session.

No business logic — delegates to CacheClient (SKILL-B).
No credentials stored — SessionRecord excludes passwords/API keys (SKILL-D R-D5).
"""

from fastapi import APIRouter

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.observability.logger import get_logger
from api.routers.session_models import (
    DeleteSessionResponse,
    GetSessionResponse,
    SaveSessionRequest,
    SaveSessionResponse,
    SessionRecord,
)

logger = get_logger(__name__)

router = APIRouter(tags=["session"])


@router.get("/{user_hash}", response_model=GetSessionResponse)
async def get_session(user_hash: str) -> GetSessionResponse:
    """Retrieve a persisted session record.

    Returns found=True with the record on cache hit, found=False on miss.
    Never raises — CacheClient degrades gracefully (SKILL-D R-D9).
    """
    cache = get_cache_client()
    key = CacheKey.session(user_hash=user_hash)
    record = await cache.get(key, model=SessionRecord)

    if record is None:
        logger.debug("session_not_found", user_hash=user_hash[:16])
        return GetSessionResponse(
            run_id="",
            status="success",
            found=False,
            record=None,
        )

    logger.info(
        "session_restored",
        user_hash=user_hash[:16],
        run_id=record.run_id[:16],
        phase=record.phase,
    )
    return GetSessionResponse(
        run_id=record.run_id,
        status="success",
        found=True,
        record=record,
    )


@router.post("/save", response_model=SaveSessionResponse)
async def save_session(body: SaveSessionRequest) -> SaveSessionResponse:
    """Persist a session record to Redis (no TTL — lives until explicit delete)."""
    cache = get_cache_client()
    key = CacheKey.session(user_hash=body.user_hash)
    ok = await cache.set(key, body.record, ttl=None)

    logger.info(
        "session_saved",
        user_hash=body.user_hash[:16],
        run_id=body.record.run_id[:16],
        phase=body.record.phase,
        success=ok,
    )
    return SaveSessionResponse(
        run_id=body.record.run_id,
        status="success" if ok else "error",
        saved=ok,
    )


@router.delete("/{user_hash}", response_model=DeleteSessionResponse)
async def delete_session(user_hash: str) -> DeleteSessionResponse:
    """Delete a persisted session record (used by "New Session" in the UI)."""
    cache = get_cache_client()
    key = CacheKey.session(user_hash=user_hash)
    ok = await cache.delete(key)

    logger.info("session_deleted", user_hash=user_hash[:16], success=ok)
    return DeleteSessionResponse(
        run_id="",
        status="success",
        deleted=ok,
    )
