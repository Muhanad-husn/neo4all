"""
api/routers/documents.py — POST /api/documents/ingest,
GET /api/documents/{run_id}, GET /api/documents/{run_id}/{doc_id}/chunks
(SPEC-03 S-03.6).

Ingestion pipeline (POST /ingest)
----------------------------------
1. Validate run_id and source_identity (IngestDocumentRequest).
2. Read uploaded file bytes.
3. IngestorService.ingest_document() — three-tier parser fallback chain.
   Cache hit: document unchanged + same parser config → return immediately
              (chunks already exist in Qdrant; manifest in Redis/S3).
4. ArtifactsService.store_raw_document() — non-fatal on failure.
5. _text_to_elements() — adapt extracted text to ParsedElement list.
6. ChunkingService.chunk_document() — semantic chunking.
7. VectorIndexer.index_chunks() — non-fatal (Qdrant is evidence-only).
8. DocumentManifest creation + IngestorService.finalize_manifest().
   Failure here → status="partial" (207).
9. Return IngestDocumentResponse with quality summary.

Architecture
------------
Services injected via FastAPI Depends() singleton factories (thin router
pattern, SKILL-B).  Models in api/routers/documents_models.py (SKILL-A R-A5).
Helper adapters in api/routers/documents_helpers.py (SKILL-B R-B7).

Path ordering
-------------
Static sub-paths (/ingest) are registered before parameterised routes
(/{run_id}, /{run_id}/{doc_id}/chunks) to prevent spurious parameter matches.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from pydantic import ValidationError

from api.models.document import DocumentManifest
from api.models.responses import ErrorDetail
from api.observability.logger import get_logger
from api.routers.documents_helpers import (
    _compute_parser_config_hash,
    _compute_quality_flags_summary,
    _compute_quality_summary,
    _text_to_elements,
)
from api.routers.documents_models import (
    ChunkOut,
    ChunksResponse,
    DocumentSummary,
    IngestDocumentRequest,
    IngestDocumentResponse,
    ListDocumentsResponse,
)
from api.services.chunking import ChunkingError, ChunkingService, get_chunking_service
from api.services.ingestion import IngestionError, IngestorService, get_ingestor_service
from api.storage.artifacts import ArtifactsService, StorageError, get_artifacts_service
from api.vector.indexer import VectorIndexer, get_vector_indexer

logger = get_logger(__name__)

router = APIRouter(tags=["documents"])


# ---------------------------------------------------------------------------
# POST /api/documents/ingest
# ---------------------------------------------------------------------------


@router.post(
    "/ingest",
    response_model=IngestDocumentResponse,
    summary="Parse, chunk, and index an uploaded document for a run",
    responses={
        207: {
            "model": IngestDocumentResponse,
            "description": "Chunks indexed but manifest store failed (partial success)",
        },
        422: {
            "model": IngestDocumentResponse,
            "description": "All parser tiers failed or chunking hard-rejected the document",
        },
    },
)
async def ingest_document(
    response: Response,
    run_id: str = Form(..., description="Governed run identifier"),
    source_identity: str = Form(
        ...,
        description=(
            "Stable, content-independent source name (e.g. original filename). "
            "Re-uploading the same file with the same source_identity enables "
            "incremental reruns — the parse step is skipped when content and "
            "parser configuration are unchanged."
        ),
    ),
    file: UploadFile = File(..., description="Document to ingest (PDF or DOCX)"),
    ingestor: IngestorService = Depends(get_ingestor_service),
    artifacts: ArtifactsService = Depends(get_artifacts_service),
    chunker: ChunkingService = Depends(get_chunking_service),
    indexer: VectorIndexer = Depends(get_vector_indexer),
) -> IngestDocumentResponse:
    """Parse, chunk, and index an uploaded document.

    Drives the full SPEC-03 ingestion pipeline: Docling → Unstructured →
    raw text fallback parsing; semantic chunking; Qdrant vector indexing;
    S3/Redis manifest persistence.

    Returns immediately (status="success") on a manifest cache hit — the
    document was previously ingested with identical content and parser
    configuration, so no re-parsing is needed.

    Returns status="partial" (HTTP 207) when chunks were indexed but the
    S3 manifest write failed — the document is usable for extraction but
    will be re-parsed on the next upload.
    """
    # --- Validate string form fields via Pydantic (SKILL-A R-A4) ---
    try:
        req = IngestDocumentRequest(run_id=run_id, source_identity=source_identity)
    except ValidationError as exc:
        logger.warning(
            "ingest_validation_failed",
            run_id=run_id,
            error=str(exc),
        )
        response.status_code = 422
        return IngestDocumentResponse(
            run_id=run_id,
            status="error",
            errors=[ErrorDetail(code="validation_failed", message=str(exc))],
        )

    logger.info(
        "ingest_requested",
        run_id=req.run_id,
        source_identity=req.source_identity,
        content_type=file.content_type,
    )

    # --- Read file bytes ---
    file_bytes: bytes = await file.read()
    if not file_bytes:
        logger.warning(
            "ingest_empty_file",
            run_id=req.run_id,
            source_identity=req.source_identity,
        )
        response.status_code = 422
        return IngestDocumentResponse(
            run_id=req.run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="empty_file",
                    message="Uploaded file is empty. A non-empty PDF or DOCX file is required.",
                )
            ],
        )

    # --- Parse: three-tier fallback chain ---
    try:
        result = await ingestor.ingest_document(
            run_id=req.run_id,
            file_bytes=file_bytes,
            source_identity=req.source_identity,
        )
    except IngestionError as exc:
        logger.error(
            "ingest_pipeline_failed",
            run_id=req.run_id,
            source_identity=req.source_identity,
            code=exc.code,
        )
        response.status_code = 422
        return IngestDocumentResponse(
            run_id=req.run_id,
            status="error",
            errors=[ErrorDetail(code=exc.code, message=exc.message)],
        )

    doc_id = result.document.doc_id

    # --- Cache hit: valid manifest found — skip the full pipeline ---
    if result.existing_manifest is not None:
        m = result.existing_manifest
        logger.info(
            "ingest_cache_hit",
            run_id=req.run_id,
            doc_id=doc_id,
            chunk_count=len(m.chunk_ids),
        )
        return IngestDocumentResponse(
            run_id=req.run_id,
            status="success",
            doc_id=doc_id,
            chunk_count=len(m.chunk_ids),
            parser_used=result.document.parser_used,
            # quality_summary=None: not re-computed on cache hit
        )

    # --- Store raw document bytes (non-fatal: file is recoverable by re-upload) ---
    try:
        await artifacts.store_raw_document(req.run_id, doc_id, file_bytes)
    except StorageError as exc:
        logger.warning(
            "raw_document_store_failed",
            run_id=req.run_id,
            doc_id=doc_id,
            error=exc.message,
        )

    # --- Convert extracted text to ParsedElements for ChunkingService ---
    elements = _text_to_elements(result.extracted_text)

    # --- Chunk ---
    try:
        chunks = chunker.chunk_document(
            document=result.document,
            elements=elements,
            default_quality_flags=result.default_quality_flags,
        )
    except ChunkingError as exc:
        logger.error(
            "ingest_chunking_failed",
            run_id=req.run_id,
            doc_id=doc_id,
            code=exc.code,
        )
        response.status_code = 422
        return IngestDocumentResponse(
            run_id=req.run_id,
            status="error",
            errors=[ErrorDetail(code=exc.code, message=exc.message)],
        )

    # --- Index in Qdrant (evidence-only; VectorIndexer never raises) ---
    await indexer.index_chunks(run_id=req.run_id, chunks=chunks)

    # --- Build and persist manifest ---
    manifest = DocumentManifest(
        doc_id=doc_id,
        run_id=req.run_id,
        content_hash=result.document.content_hash,
        parser_config_hash=_compute_parser_config_hash(),
        chunk_ids=tuple(c.chunk_id for c in chunks),
        timestamp=datetime.now(UTC),
    )

    try:
        await ingestor.finalize_manifest(manifest)
    except (IngestionError, StorageError) as exc:
        # Partial success: chunks exist in Qdrant but incremental reruns will
        # re-parse because the manifest was not persisted.
        logger.error(
            "ingest_manifest_store_failed",
            run_id=req.run_id,
            doc_id=doc_id,
            error=str(exc),
        )
        response.status_code = 207
        return IngestDocumentResponse(
            run_id=req.run_id,
            status="partial",
            doc_id=doc_id,
            chunk_count=len(chunks),
            parser_used=result.document.parser_used,
            quality_summary=_compute_quality_summary(chunks),
            errors=[ErrorDetail(code="manifest_store_failed", message=str(exc))],
        )

    logger.info(
        "ingest_complete",
        run_id=req.run_id,
        doc_id=doc_id,
        chunk_count=len(chunks),
        parser_used=result.document.parser_used,
    )

    return IngestDocumentResponse(
        run_id=req.run_id,
        status="success",
        doc_id=doc_id,
        chunk_count=len(chunks),
        parser_used=result.document.parser_used,
        quality_summary=_compute_quality_summary(chunks),
    )


# ---------------------------------------------------------------------------
# GET /api/documents/{run_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{run_id}",
    response_model=ListDocumentsResponse,
    summary="List all successfully ingested documents for a run",
    responses={
        503: {
            "model": ListDocumentsResponse,
            "description": "S3 storage unavailable",
        },
    },
)
async def list_documents(
    run_id: str,
    response: Response,
    artifacts: ArtifactsService = Depends(get_artifacts_service),
) -> ListDocumentsResponse:
    """Return all documents with a finalised manifest for this run.

    Scans S3 for manifests under the run's namespace prefix (Redis-first per
    document, S3 fallback).  Documents whose manifest write failed are excluded.
    Results are sorted by creation timestamp, newest first.
    """
    logger.info("list_documents_requested", run_id=run_id)

    try:
        manifests = await artifacts.list_manifests_for_run(run_id)
    except StorageError as exc:
        logger.error(
            "list_documents_storage_error",
            run_id=run_id,
            error=exc.message,
        )
        response.status_code = 503
        return ListDocumentsResponse(
            run_id=run_id,
            status="error",
            errors=[ErrorDetail(code=exc.code, message=exc.message)],
        )

    manifests.sort(key=lambda m: m.timestamp, reverse=True)

    documents = [
        DocumentSummary(
            doc_id=m.doc_id,
            chunk_count=len(m.chunk_ids),
            created_at=m.timestamp,
        )
        for m in manifests
    ]

    logger.info(
        "list_documents_success",
        run_id=run_id,
        total_count=len(documents),
    )

    return ListDocumentsResponse(
        run_id=run_id,
        status="success",
        documents=documents,
        total_count=len(documents),
    )


# ---------------------------------------------------------------------------
# GET /api/documents/{run_id}/{doc_id}/chunks
# ---------------------------------------------------------------------------


@router.get(
    "/{run_id}/{doc_id}/chunks",
    response_model=ChunksResponse,
    summary="Return chunk metadata for a document with quality flag highlights",
    responses={
        404: {
            "model": ChunksResponse,
            "description": "No manifest found for doc_id in this run",
        },
        503: {
            "model": ChunksResponse,
            "description": "S3 storage unavailable",
        },
    },
)
async def get_document_chunks(
    run_id: str,
    doc_id: str,
    response: Response,
    artifacts: ArtifactsService = Depends(get_artifacts_service),
    indexer: VectorIndexer = Depends(get_vector_indexer),
) -> ChunksResponse:
    """Return chunk metadata for a single document with quality highlights.

    Verifies the document exists via its DocumentManifest (S3/Redis), then
    retrieves chunk metadata from Qdrant (evidence-only).  If Qdrant is
    unavailable the chunks list is empty but no error is returned.

    chunk.text is not included — char_count is returned instead (SKILL-D R-D5).
    Chunks are sorted by start_page_locator ("p{n}:c{i}") in ascending order.
    """
    logger.info("get_chunks_requested", run_id=run_id, doc_id=doc_id)

    # --- Verify document exists via its manifest ---
    try:
        manifest = await artifacts.retrieve_manifest(run_id, doc_id)
    except StorageError as exc:
        logger.error(
            "get_chunks_storage_error",
            run_id=run_id,
            doc_id=doc_id,
            error=exc.message,
        )
        response.status_code = 503
        return ChunksResponse(
            run_id=run_id,
            status="error",
            errors=[ErrorDetail(code=exc.code, message=exc.message)],
        )

    if manifest is None:
        logger.warning("get_chunks_not_found", run_id=run_id, doc_id=doc_id)
        response.status_code = 404
        return ChunksResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="document_not_found",
                    message=(
                        f"No manifest found for doc_id={doc_id!r} in run {run_id!r}. "
                        "The document may not have been successfully ingested."
                    ),
                )
            ],
        )

    # --- Retrieve chunk payloads from Qdrant (evidence-only, never raises) ---
    raw_chunks = await indexer.get_chunks_for_doc(run_id=run_id, doc_id=doc_id)

    chunks = [
        ChunkOut(
            chunk_id=c["chunk_id"],
            start_page=c["start_page"],
            end_page=c["end_page"],
            start_page_locator=c["start_page_locator"],
            quality_flags=c["quality_flags"],
            char_count=c["text_length"],
        )
        for c in raw_chunks
    ]

    summary = _compute_quality_flags_summary(chunks)

    logger.info(
        "get_chunks_success",
        run_id=run_id,
        doc_id=doc_id,
        chunk_count=len(chunks),
    )

    return ChunksResponse(
        run_id=run_id,
        status="success",
        chunks=chunks,
        quality_flags_summary=summary,
    )
