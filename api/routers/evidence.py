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

Models and helpers are in api/routers/evidence_models.py (SKILL-B R-B7 split).

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

from fastapi import APIRouter, Query, Response

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.models.responses import ErrorDetail
from api.observability.logger import get_logger
from api.routers.evidence_models import (
    EvidenceCandidateResponse,
    EvidenceQueryRequest,
    EvidenceQueryResponse,
    _SlimCandidateCache,
    _DEFAULT_TOP_K,
    _MAX_TOP_K,
    _detection_hash,
    _error_response,
    _merge_chunks,
    _to_chunk_out,
)
from api.schema.models import SchemaVersion
from api.vector.retriever import EvidenceChunk, EvidenceRetriever

logger = get_logger(__name__)

router = APIRouter(tags=["evidence"])


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
