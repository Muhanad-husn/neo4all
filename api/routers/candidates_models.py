"""
api/routers/candidates_models.py — Request/response models for candidate endpoints.

These Pydantic models define the API contracts for the candidate generation,
listing, and clearing endpoints.  Extracted from candidates.py to keep the
router module focused on HTTP handling (matching the approvals_models.py
pattern).

Also houses the shared ``_ExcludedCandidatesCache`` model and the
``load_excluded_ids`` / ``add_excluded_id`` helpers, which are used by both
candidates.py and approvals.py.

All response models extend BaseResponse (SKILL-A R-A2).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.models.responses import BaseResponse
from api.observability.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Generate candidates
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


# ---------------------------------------------------------------------------
# Candidate serialisation
# ---------------------------------------------------------------------------


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
# Clear candidates
# ---------------------------------------------------------------------------


class ClearCandidatesResponse(BaseResponse):
    """Response for DELETE /api/curation/candidates/{run_id}.

    Attributes:
        candidates_cleared:  Number of candidate cache keys deleted.
        proposals_cleared:   Number of proposals deleted.
        graph_cache_cleared: Number of graph query cache keys deleted.
    """

    candidates_cleared: int = 0
    proposals_cleared: int = 0
    graph_cache_cleared: int = 0


# ---------------------------------------------------------------------------
# Cache envelopes — internal schema, not exposed via API
# ---------------------------------------------------------------------------


class _CandidateListCache(BaseModel):
    """Redis cache envelope for a run's candidate list."""

    candidates: list[CandidateOut]
    schema_version: str


class _ExcludedCandidatesCache(BaseModel):
    """Redis cache envelope for the set of excluded candidate_ids within a run.

    Shared between candidates.py and approvals.py — consolidated here to
    avoid duplication.
    """

    candidate_ids: list[str]


# ---------------------------------------------------------------------------
# Excluded candidates helpers
# ---------------------------------------------------------------------------


async def load_excluded_ids(run_id: str) -> set[str]:
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


async def add_excluded_id(run_id: str, candidate_id: str) -> None:
    """Add a candidate_id to the excluded set in Redis.

    Fails silently on cache errors (SKILL-D R-D9).
    """
    try:
        cache = get_cache_client()
        cache_key = CacheKey.excluded_candidates(run_id)
        existing: _ExcludedCandidatesCache | None = await cache.get(
            cache_key, model=_ExcludedCandidatesCache,
        )
        current_ids = set(existing.candidate_ids) if existing else set()
        current_ids.add(candidate_id)
        await cache.set(
            cache_key,
            _ExcludedCandidatesCache(candidate_ids=sorted(current_ids)),
        )
    except Exception:
        logger.warning(
            "excluded_candidates_add_failed",
            run_id=run_id,
            candidate_id=candidate_id,
        )


async def remove_excluded_id(run_id: str, candidate_id: str) -> None:
    """Remove a candidate_id from the excluded set in Redis.

    Fails silently on cache errors (SKILL-D R-D9).
    """
    try:
        cache = get_cache_client()
        cache_key = CacheKey.excluded_candidates(run_id)
        existing: _ExcludedCandidatesCache | None = await cache.get(
            cache_key, model=_ExcludedCandidatesCache,
        )
        if existing:
            current_ids = set(existing.candidate_ids)
            current_ids.discard(candidate_id)
            await cache.set(
                cache_key,
                _ExcludedCandidatesCache(candidate_ids=sorted(current_ids)),
            )
    except Exception:
        logger.warning(
            "excluded_candidates_remove_failed",
            run_id=run_id,
            candidate_id=candidate_id,
        )
