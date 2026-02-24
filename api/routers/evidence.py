"""
api/routers/evidence.py — Evidence retrieval endpoints (SPEC-06 S-06.6).

Exposes Qdrant evidence for human curation decisions via two endpoints:

  GET  /evidence/{candidate_id}?run_id=...
      Retrieve ranked evidence chunks for all graph elements referenced by a
      specific candidate.  Evidence is retrieved by dedupe_key (semantic search
      seeded from each element's primary value) and merged by chunk_id, keeping
      the highest relevance_score per chunk.

  POST /evidence/query
      Ad-hoc evidence query against a run's Qdrant collection.  Supports three
      retrieval modes:
        dedupe_key — semantic search seeded from a graph element's primary value
        doc        — scroll all chunks from a specific source document
        semantic   — free-text semantic similarity search

Architecture (SKILL-B: thin router)
--------------------------------------
All retrieval logic lives in api/vector/retriever.py (EvidenceRetriever).
This file coordinates the call, resolves the candidate from the candidate
cache, and maps results to typed response models.

Error handling
--------------
GET /evidence/{candidate_id}:
  404 — schema not locked, candidate cache empty, or candidate_id not found.
  200 — success (may be empty chunks list if Qdrant has no data for this run).

POST /evidence/query:
  409 — schema not locked for run_id.
  400 — invalid mode or missing mode-required field.
  200 — success (may be empty list on Qdrant error or no results).

Sensitive data (SKILL-D R-D5):
  Chunk text is never logged — only chunk_id.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, field_validator

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.models.responses import BaseResponse, ErrorDetail
from api.observability.logger import get_logger
from api.schema.models import SchemaVersion
from api.vector.retriever import EvidenceChunk, EvidenceRetriever

logger = get_logger(__name__)

router = APIRouter(tags=["evidence"])

_MAX_TOP_K: int = 50
_DEFAULT_TOP_K: int = 10
_MAX_DOC_LIMIT: int = 200


# ---------------------------------------------------------------------------
# Slim cache models — read from the candidate cache without importing curation
# (avoids module-level cross-dependency; Pydantic v2 silently drops extra fields)
# ---------------------------------------------------------------------------


class _SlimCandidate(BaseModel):
    """Minimum candidate fields needed for evidence retrieval."""

    candidate_id: str
    involved_element_refs: list[str]


class _SlimCandidateCache(BaseModel):
    """Slim envelope for the candidate list stored by curation.py."""

    candidates: list[_SlimCandidate]
    schema_version: str


# ---------------------------------------------------------------------------
# Request / Response models (SKILL-A R-A1, R-A2)
# ---------------------------------------------------------------------------


class EvidenceChunkOut(BaseModel):
    """API-safe serialisation of an EvidenceChunk.

    All fields are plain Python types — no internal types cross the boundary
    (SKILL-A R-A3).
    """

    chunk_id: str
    doc_id: str
    run_id: str
    text: str
    start_page_locator: str
    start_page: int
    end_page: int
    quality_flags: list[str]
    relevance_score: float


class EvidenceCandidateResponse(BaseResponse):
    """Response for GET /evidence/{candidate_id}.

    Attributes:
        candidate_id:  The candidate whose evidence was retrieved.
        chunks:        Evidence chunks sorted by descending relevance_score.
        dedupe_keys:   The element dedupe_keys that were queried.
    """

    candidate_id: str = ""
    chunks: list[EvidenceChunkOut] = []
    dedupe_keys: list[str] = []


class EvidenceQueryRequest(BaseModel):
    """Request body for POST /evidence/query.

    Exactly one retrieval mode must be chosen, and the corresponding key field
    must be non-empty.

    mode:
        dedupe_key — semantic search seeded from a graph element dedupe_key.
        doc        — scroll all chunks from a specific source document.
        semantic   — free-text semantic similarity search.
    """

    run_id: str
    mode: Literal["dedupe_key", "doc", "semantic"]

    # --- mode-specific input fields ---
    dedupe_key: str = ""
    doc_id: str = ""
    query_text: str = ""

    # --- retrieval limits ---
    top_k: int = _DEFAULT_TOP_K
    doc_limit: int = 50
    doc_offset: int = 0

    @field_validator("run_id")
    @classmethod
    def _non_empty_run_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("run_id must be a non-empty, non-whitespace string")
        return v

    @field_validator("top_k")
    @classmethod
    def _clamp_top_k(cls, v: int) -> int:
        return max(1, min(v, _MAX_TOP_K))

    @field_validator("doc_limit")
    @classmethod
    def _clamp_doc_limit(cls, v: int) -> int:
        return max(1, min(v, _MAX_DOC_LIMIT))

    @field_validator("doc_offset")
    @classmethod
    def _non_negative_offset(cls, v: int) -> int:
        return max(0, v)


class EvidenceQueryResponse(BaseResponse):
    """Response for POST /evidence/query.

    Attributes:
        mode:   The retrieval mode that was executed.
        chunks: Evidence chunks ordered by relevance (or payload order for doc mode).
    """

    mode: str = ""
    chunks: list[EvidenceChunkOut] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detection_hash(schema_version: str) -> str:
    """Deterministic 32-char hash matching curation.py's candidate cache keys."""
    payload = f"{schema_version}:all_detectors_v1"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _to_chunk_out(chunk: EvidenceChunk) -> EvidenceChunkOut:
    """Convert an EvidenceChunk to its API-safe representation."""
    return EvidenceChunkOut(
        chunk_id=chunk.chunk_id,
        doc_id=chunk.doc_id,
        run_id=chunk.run_id,
        text=chunk.text,
        start_page_locator=chunk.start_page_locator,
        start_page=chunk.start_page,
        end_page=chunk.end_page,
        quality_flags=chunk.quality_flags,
        relevance_score=chunk.relevance_score,
    )


def _merge_chunks(chunk_lists: list[list[EvidenceChunk]]) -> list[EvidenceChunkOut]:
    """Merge evidence from multiple dedupe_key queries, deduplicating by chunk_id.

    When the same chunk appears in multiple lists, the highest relevance_score
    is retained.  Results are sorted by descending relevance_score then chunk_id
    for deterministic ordering.
    """
    best: dict[str, EvidenceChunk] = {}
    for chunks in chunk_lists:
        for chunk in chunks:
            existing = best.get(chunk.chunk_id)
            if existing is None or chunk.relevance_score > existing.relevance_score:
                best[chunk.chunk_id] = chunk

    merged = sorted(
        best.values(),
        key=lambda c: (-c.relevance_score, c.chunk_id),
    )
    return [_to_chunk_out(c) for c in merged]


def _error_response(
    run_id: str,
    code: str,
    message: str,
    candidate_id: str = "",
) -> EvidenceCandidateResponse:
    """Convenience builder for structured 404 error responses."""
    return EvidenceCandidateResponse(
        run_id=run_id,
        candidate_id=candidate_id,
        status="error",
        errors=[ErrorDetail(code=code, message=message)],
    )


# ---------------------------------------------------------------------------
# GET /evidence/{candidate_id}
# ---------------------------------------------------------------------------


@router.get(
    "/evidence/{candidate_id}",
    response_model=EvidenceCandidateResponse,
    summary="Retrieve ranked evidence for all elements referenced by a candidate",
    responses={
        404: {
            "model": EvidenceCandidateResponse,
            "description": "Schema not locked, cache empty, or candidate not found",
        },
    },
)
async def get_evidence_for_candidate(
    candidate_id: str,
    response: Response,
    run_id: str = Query(..., description="Governed run identifier"),
    top_k: int = Query(_DEFAULT_TOP_K, ge=1, le=_MAX_TOP_K),
) -> EvidenceCandidateResponse:
    """Return ranked evidence chunks for all graph elements in a candidate.

    Evidence is retrieved by semantic search seeded from each element's
    primary value (parsed from its dedupe_key).  Results from multiple
    elements are merged by chunk_id; duplicates keep the highest score.

    Requires the candidate cache to be populated via POST /candidates/generate.

    **Errors**
    - ``404 Not Found`` — schema not locked, candidate cache empty, or
      candidate_id not found in the cached results.
    """
    logger.info(
        "evidence_candidate_requested",
        run_id=run_id,
        candidate_id=candidate_id,
    )

    cache = get_cache_client()

    # 1. Resolve schema — needed to derive the candidate cache key.
    schema: SchemaVersion | None = await cache.get(
        CacheKey.schema(run_id=run_id), model=SchemaVersion
    )
    if schema is None:
        logger.warning(
            "evidence_schema_not_locked", run_id=run_id, candidate_id=candidate_id
        )
        response.status_code = 404
        return _error_response(
            run_id=run_id,
            candidate_id=candidate_id,
            code="schema_not_locked",
            message=f"No locked schema found for run '{run_id}'.",
        )

    # 2. Look up candidate in the detection cache.
    dhash = _detection_hash(schema.version_hash)
    cache_key = CacheKey.candidates(run_id=run_id, detection_hash=dhash)
    cached: _SlimCandidateCache | None = await cache.get(
        cache_key, model=_SlimCandidateCache
    )
    if cached is None:
        logger.warning(
            "evidence_candidate_cache_empty",
            run_id=run_id,
            candidate_id=candidate_id,
        )
        response.status_code = 404
        return _error_response(
            run_id=run_id,
            candidate_id=candidate_id,
            code="candidates_not_generated",
            message=(
                f"No candidate cache found for run '{run_id}'. "
                "Run POST /candidates/generate first."
            ),
        )

    candidate = next(
        (c for c in cached.candidates if c.candidate_id == candidate_id), None
    )
    if candidate is None:
        logger.warning(
            "evidence_candidate_not_found",
            run_id=run_id,
            candidate_id=candidate_id,
        )
        response.status_code = 404
        return _error_response(
            run_id=run_id,
            candidate_id=candidate_id,
            code="candidate_not_found",
            message=f"Candidate '{candidate_id}' not found in cache for run '{run_id}'.",
        )

    # 3. Retrieve evidence for each involved element (graceful on Qdrant errors).
    dedupe_keys = candidate.involved_element_refs
    retriever = EvidenceRetriever()
    chunk_lists: list[list[EvidenceChunk]] = []
    for dk in dedupe_keys:
        chunks = await retriever.by_dedupe_key(
            run_id=run_id, dedupe_key=dk, top_k=top_k
        )
        chunk_lists.append(chunks)

    merged = _merge_chunks(chunk_lists)

    logger.info(
        "evidence_candidate_success",
        run_id=run_id,
        candidate_id=candidate_id,
        dedupe_key_count=len(dedupe_keys),
        chunk_count=len(merged),
    )

    return EvidenceCandidateResponse(
        run_id=run_id,
        candidate_id=candidate_id,
        status="success",
        chunks=merged,
        dedupe_keys=list(dedupe_keys),
    )


# ---------------------------------------------------------------------------
# POST /evidence/query
# ---------------------------------------------------------------------------


@router.post(
    "/evidence/query",
    response_model=EvidenceQueryResponse,
    summary="Ad-hoc evidence retrieval by dedupe_key, doc_id, or free-text query",
    responses={
        400: {
            "model": EvidenceQueryResponse,
            "description": "Missing mode-specific field",
        },
        409: {
            "model": EvidenceQueryResponse,
            "description": "Schema not locked for this run_id",
        },
    },
)
async def query_evidence(
    request: EvidenceQueryRequest,
    response: Response,
) -> EvidenceQueryResponse:
    """Execute an ad-hoc evidence query against a run's Qdrant collection.

    Choose the retrieval mode that fits the question:
    - ``dedupe_key``: semantic search seeded from a specific graph element.
    - ``doc``:        scroll all chunks from a single source document.
    - ``semantic``:   free-text semantic similarity search.

    **Errors**
    - ``409 Conflict`` — schema not locked for this run_id.
    - ``400 Bad Request`` — mode-specific required field is empty.
    """
    run_id = request.run_id
    mode = request.mode

    logger.info("evidence_query_requested", run_id=run_id, mode=mode)

    cache = get_cache_client()

    # Verify schema is locked — confirms run_id is a governed run.
    schema: SchemaVersion | None = await cache.get(
        CacheKey.schema(run_id=run_id), model=SchemaVersion
    )
    if schema is None:
        logger.warning("evidence_query_schema_not_locked", run_id=run_id)
        response.status_code = 409
        return EvidenceQueryResponse(
            run_id=run_id,
            mode=mode,
            status="error",
            errors=[
                ErrorDetail(
                    code="schema_not_locked",
                    message=f"No locked schema found for run '{run_id}'.",
                )
            ],
        )

    retriever = EvidenceRetriever()
    chunks: list[EvidenceChunk] = []

    if mode == "dedupe_key":
        if not request.dedupe_key.strip():
            response.status_code = 400
            return EvidenceQueryResponse(
                run_id=run_id,
                mode=mode,
                status="error",
                errors=[
                    ErrorDetail(
                        code="missing_dedupe_key",
                        message="mode 'dedupe_key' requires a non-empty dedupe_key field.",
                    )
                ],
            )
        chunks = await retriever.by_dedupe_key(
            run_id=run_id,
            dedupe_key=request.dedupe_key,
            top_k=request.top_k,
        )

    elif mode == "doc":
        if not request.doc_id.strip():
            response.status_code = 400
            return EvidenceQueryResponse(
                run_id=run_id,
                mode=mode,
                status="error",
                errors=[
                    ErrorDetail(
                        code="missing_doc_id",
                        message="mode 'doc' requires a non-empty doc_id field.",
                    )
                ],
            )
        chunks = await retriever.by_doc(
            run_id=run_id,
            doc_id=request.doc_id,
            limit=request.doc_limit,
            offset=request.doc_offset,
        )

    else:  # semantic
        if not request.query_text.strip():
            response.status_code = 400
            return EvidenceQueryResponse(
                run_id=run_id,
                mode=mode,
                status="error",
                errors=[
                    ErrorDetail(
                        code="missing_query_text",
                        message="mode 'semantic' requires a non-empty query_text field.",
                    )
                ],
            )
        chunks = await retriever.by_query(
            run_id=run_id,
            query_text=request.query_text,
            top_k=request.top_k,
        )

    logger.info(
        "evidence_query_success",
        run_id=run_id,
        mode=mode,
        chunk_count=len(chunks),
    )

    return EvidenceQueryResponse(
        run_id=run_id,
        mode=mode,
        status="success",
        chunks=[_to_chunk_out(c) for c in chunks],
    )
