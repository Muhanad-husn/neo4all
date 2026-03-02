# SPEC-03: Document Ingestion & Chunking (Phase 2)

**Increment**: 3 | **Version target**: 0.3.0 | **Prerequisites**: SPEC-02 complete
**Skills required**: SKILL-A, SKILL-B, SKILL-D

---

## Objective

Implement Phase 2: users upload documents, the system parses and chunks them with a three-tier fallback chain (Docling → Unstructured → raw text), indexes chunks in Qdrant, and generates a manifest for incremental reruns.

---

## Specifications

### S-03.1: Parser Fallback Chain
Create `api/services/ingestion.py` — `IngestorService` with three-tier parsing:
1. **Primary — Docling**: Full structural parsing (titles, paragraphs, tables, images, captions).
2. **Secondary — Unstructured**: Activated if Docling raises an exception or returns empty/null.
3. **Tertiary — Raw text**: PyPDF2 (PDF), python-docx (DOCX), or direct UTF-8/Latin-1 decode (plain-text formats: .txt, .md, .csv, .html, etc.). Flat text, no structural metadata. All chunks get `quality_flag: "raw_fallback"`.

Each fallback transition logged as structured event with reason (SKILL-D). `parser_used` recorded in manifest. All three fail → hard-reject with structured error. Parsers individually toggleable via `api/config.py`.

### S-03.2: Chunking
Create `api/services/chunking.py`: semantic grouping respecting headings, table isolation, configurable char/token limits. `chunk_id = hash(doc_id + start_page_locator)`. Hard-reject on missing IDs/locator. Soft-flag on low OCR confidence or text density.

### S-03.3: Document & Chunk Models
Create `api/models/document.py`: `Document`, `Chunk`, `DocumentManifest`, `ChunkQualityFlag` enum (low_ocr_confidence, low_text_density, raw_fallback).

### S-03.4: Storage
Create `api/storage/artifacts.py`: boto3 wrapper — `store_raw_document()`, `store_manifest()`, `retrieve_manifest()`. Cache manifest lookups via `CacheKey.manifest(run_id, doc_id)` (SKILL-D R-D8). Manifest enables incremental reruns.

### S-03.5: Vector Indexing
Create `api/vector/indexer.py`: embed chunk text, upsert to Qdrant with metadata (chunk_id, doc_id, run_id, page locator, quality_flags). Evidence-only.

### S-03.6: Endpoints
Create `api/routers/documents.py`: `POST /api/documents/ingest`, `GET /api/documents/{run_id}`, `GET /api/documents/{run_id}/{doc_id}/chunks`. SKILL-A models.

### S-03.7: UI
Create `ui/pages/ingestion.py`: Phase 2 — upload, progress, chunk manifest with quality flags, advance when ≥1 doc ingested.

---

## Files to Generate

| # | File Path | Purpose |
|---|-----------|---------|
| 1 | `api/models/document.py` | Document/chunk models |
| 2 | `api/services/ingestion.py` | Parser fallback chain |
| 3 | `api/services/chunking.py` | Chunking logic |
| 4 | `api/storage/artifacts.py` | S3 storage |
| 5 | `api/vector/indexer.py` | Qdrant indexing |
| 6 | `api/routers/documents.py` | Ingestion endpoints |
| 7 | `ui/pages/ingestion.py` | Phase 2 UI |
| 8 | `fixtures/sample.pdf` | Test PDF |
| 9 | `fixtures/sample.docx` | Test DOCX |
| 10 | `tests/unit/test_doc_id.py` | Deterministic doc_id |
| 11 | `tests/unit/test_chunk_id.py` | Deterministic chunk_id |
| 12 | `tests/unit/test_chunking.py` | Chunking boundaries |
| 13 | `tests/unit/test_ingestion_fallback.py` | Fallback chain |
| 14 | `tests/unit/test_manifest.py` | Manifest idempotency |

---

## Acceptance Criteria

- [ ] Docling parses test documents with structural metadata
- [ ] Mocked Docling failure → Unstructured activates
- [ ] Both fail → raw fallback with `quality_flag: "raw_fallback"`
- [ ] All three fail → hard-reject with structured error
- [ ] `parser_used` in manifest correct
- [ ] doc_id and chunk_id deterministic; manifest idempotent
- [ ] Chunks indexed in Qdrant with metadata
- [ ] Manifest lookups cached
- [ ] Fallback transitions logged as structured events
- [ ] `pyproject.toml` version `0.3.0`
- [ ] SKILL-B governance checklist passes
