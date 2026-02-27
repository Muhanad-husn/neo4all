"""
api/routers/evidence_models.py — Pydantic models and helpers for evidence endpoints.

Extracted from api/routers/evidence.py to comply with SKILL-B R-B7 (file size).
Contains request/response models (SKILL-A R-A1, R-A2), slim cache models for
reading the candidate cache without importing curation internals, and helper
functions used by the evidence route handlers.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, field_validator

from api.models.responses import BaseResponse, ErrorDetail
from api.vector.retriever import EvidenceChunk

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
        candidate_id:   The candidate whose evidence was retrieved.
        chunks:         Evidence chunks sorted by descending relevance_score.
        dedupe_keys:    The element dedupe_keys that were queried.
        qdrant_status:  Diagnostic: Qdrant collection state for this run.
                        "ok (N points)" on success, "collection_not_found" if the
                        collection does not exist, or "error: ..." on Qdrant failure.
    """

    candidate_id: str = ""
    chunks: list[EvidenceChunkOut] = []
    dedupe_keys: list[str] = []
    qdrant_status: str = ""


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
