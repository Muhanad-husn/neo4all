"""
api/routers/documents_models.py — Pydantic request/response models for document
ingestion endpoints (SKILL-A R-A1, R-A2, R-A5).

Four endpoint contracts are defined here and co-located with the route
handler (SKILL-A R-A5):

  POST   /api/documents/ingest                    → IngestDocumentRequest  / IngestDocumentResponse
  GET    /api/documents/{run_id}                   → (path param only)      / ListDocumentsResponse
  DELETE /api/documents/{run_id}/{doc_id}          → (path params only)     / DeleteDocumentResponse
  GET    /api/documents/{run_id}/{doc_id}/chunks   → (path params only)     / ChunksResponse

Shared payload types
--------------------
  QualitySummary      — per-flag chunk counts + aggregate totals for the
                        ingest response.
  DocumentSummary     — lightweight document record for the listing response
                        (sourced from DocumentManifest; no raw text).
  ChunkOut            — single chunk metadata record for the chunks endpoint.
  QualityFlagsSummary — per-flag counts for the chunks summary panel.

Error contract
--------------
All response models extend BaseResponse so every caller receives the standard
run_id / status / errors envelope (SKILL-A R-A2). Payload fields on response
models default to empty / None so that structured error responses can be
returned through the same typed model without carrying meaningless data.

Note on multipart/form-data
---------------------------
POST /api/documents/ingest accepts multipart/form-data with a binary file
upload (FastAPI UploadFile). The handler receives run_id and source_identity
as Form fields; IngestDocumentRequest validates them as a Pydantic model
inside the route handler rather than as a JSON body, satisfying SKILL-A R-A1
at the validation layer.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from api.models.responses import BaseResponse


# ---------------------------------------------------------------------------
# Shared quality payload types
# ---------------------------------------------------------------------------


class QualitySummary(BaseModel):
    """Aggregate quality statistics for a freshly ingested document.

    Returned by POST /api/documents/ingest.  Only populated on a fresh
    ingestion — cache-hit responses omit it (None) because the original
    quality data is not re-computed.

    Attributes:
        low_ocr_confidence: Chunks whose OCR confidence fell below threshold.
        low_text_density:   Chunks with fewer than the minimum character count.
        raw_fallback:       Chunks produced by the tertiary raw-text parser.
        total_chunks:       Total chunks produced from this document.
        flagged_chunks:     Chunks carrying at least one quality flag.
    """

    low_ocr_confidence: int = 0
    low_text_density: int = 0
    raw_fallback: int = 0
    total_chunks: int = 0
    flagged_chunks: int = 0


class DocumentSummary(BaseModel):
    """Lightweight document record for GET /api/documents/{run_id}.

    Sourced from the stored DocumentManifest — no raw document bytes or
    chunk text are returned. chunk_count is derived from len(chunk_ids).

    Attributes:
        doc_id:      64-char SHA-256 deterministic identifier.
        chunk_count: Number of chunks produced from this document.
        created_at:  UTC timestamp from the DocumentManifest.
    """

    doc_id: str
    chunk_count: int
    created_at: datetime
    parser_used: str = ""
    source_identity: str = ""


class ChunkOut(BaseModel):
    """Single chunk metadata record for GET /api/documents/{run_id}/{doc_id}/chunks.

    Sourced from Qdrant payload (evidence-only). chunk.text is NOT included
    in this response (it is stored in Qdrant for Agent-B retrieval only).
    char_count is returned instead so the UI can show chunk size without
    exposing raw text through the listing endpoint.

    Attributes:
        chunk_id:           64-char SHA-256 deterministic identifier.
        start_page:         0-indexed page where the chunk begins.
        end_page:           0-indexed page where the chunk ends.
        start_page_locator: Canonical position string "p{n}:c{i}".
        quality_flags:      Active quality signals (may be empty list).
        char_count:         Length of chunk.text in characters.
    """

    chunk_id: str
    start_page: int
    end_page: int
    start_page_locator: str
    quality_flags: list[str]
    char_count: int


class QualityFlagsSummary(BaseModel):
    """Per-flag chunk counts for the chunks listing endpoint.

    Returned alongside the chunks list so the UI can highlight quality
    concerns at a glance without iterating all chunks client-side.
    """

    low_ocr_confidence: int = 0
    low_text_density: int = 0
    raw_fallback: int = 0


# ---------------------------------------------------------------------------
# POST /api/documents/ingest
# ---------------------------------------------------------------------------


class IngestDocumentRequest(BaseModel):
    """Validated string fields for POST /api/documents/ingest.

    The file upload (UploadFile) is handled as a separate FastAPI Form/File
    parameter and is not modelled here. This model exists to apply non-empty
    validation on the string fields before the ingestion pipeline runs
    (SKILL-A R-A4: validation at the boundary).

    Attributes:
        run_id:          Governed run identifier.
        source_identity: Stable, content-independent source name —
                         typically the original filename or a canonical URI.
                         Two uploads of the same file should use the same
                         source_identity to enable incremental reruns.
    """

    run_id: str
    source_identity: str

    @field_validator("run_id", "source_identity")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class IngestDocumentResponse(BaseResponse):
    """Response for POST /api/documents/ingest.

    On success (fresh ingestion):
        status="success", doc_id, chunk_count, parser_used, quality_summary set.

    On cache hit (document already ingested with same content + parser config):
        status="success", doc_id, chunk_count, parser_used="cached",
        quality_summary=None (quality data not available from manifest).

    On partial success (chunks indexed but manifest store failed):
        status="partial", doc_id, chunk_count, quality_summary set,
        errors describe the manifest storage failure.

    On error:
        status="error", errors describe the failure; other fields default.
    """

    doc_id: str = ""
    chunk_count: int = 0
    parser_used: str = ""
    quality_summary: QualitySummary | None = None


# ---------------------------------------------------------------------------
# GET /api/documents/{run_id}
# ---------------------------------------------------------------------------


class ListDocumentsResponse(BaseResponse):
    """Response for GET /api/documents/{run_id}.

    Returns all documents for which a manifest exists in S3 for this run.
    Documents that were uploaded but failed before manifest finalisation
    will not appear in this list.

    Attributes:
        documents:   Ordered list of DocumentSummary records (by creation time).
        total_count: Total number of documents found.
    """

    documents: list[DocumentSummary] = []
    total_count: int = 0


# ---------------------------------------------------------------------------
# GET /api/documents/{run_id}/{doc_id}/chunks
# ---------------------------------------------------------------------------


class ChunksResponse(BaseResponse):
    """Response for GET /api/documents/{run_id}/{doc_id}/chunks.

    Returns chunk metadata sourced from Qdrant payload (evidence-only).
    Chunks are ordered by start_page_locator for stable presentation.

    Attributes:
        chunks:               Chunk metadata records (no raw text).
        quality_flags_summary: Aggregated per-flag counts across all chunks.
    """

    chunks: list[ChunkOut] = []
    quality_flags_summary: QualityFlagsSummary = Field(
        default_factory=QualityFlagsSummary
    )


# ---------------------------------------------------------------------------
# GET /api/documents/{run_id}/chunk/{chunk_id}/text
# ---------------------------------------------------------------------------


class DeleteDocumentResponse(BaseResponse):
    """Response for DELETE /api/documents/{run_id}/{doc_id}.

    Attributes:
        doc_id:         64-char SHA-256 deterministic identifier.
        chunks_deleted: Number of chunk point IDs submitted for Qdrant deletion.
        jobs_cleared:   Number of extraction job status keys cleared from cache.
    """

    doc_id: str = ""
    chunks_deleted: int = 0
    jobs_cleared: int = 0


class ChunkTextResponse(BaseResponse):
    """Response for GET /api/documents/{run_id}/chunk/{chunk_id}/text.

    Returns the raw chunk text from Qdrant for diagnostic/debugging use.
    Not intended for bulk listing — use the chunks endpoint for metadata.

    Attributes:
        chunk_id: 64-char SHA-256 deterministic identifier.
        text:     Raw chunk text, or empty string if not found.
    """

    chunk_id: str = ""
    text: str = ""
