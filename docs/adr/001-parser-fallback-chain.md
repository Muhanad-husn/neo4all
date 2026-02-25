# ADR-001: Three-Tier Parser Fallback Chain

**Status**: Accepted
**Date**: 2025-06-15
**Increment**: SPEC-03 (Document Ingestion & Chunking)

---

## Context

The platform ingests heterogeneous documents (PDF, DOCX) into a knowledge graph pipeline. Documents vary widely in quality: some are well-structured with headings, tables, and captions; others are scanned images with OCR artifacts; others are malformed or use non-standard encodings.

A single parser cannot reliably handle this diversity. Docling provides the richest structural output but fails on certain file formats and edge cases. Unstructured covers a broader range but produces less granular metadata. Raw text extraction (PyPDF2/python-docx) always succeeds on valid files but loses all structural information.

The system must maximize ingestion success rate while preserving as much structural metadata as possible for downstream extraction and curation quality.

## Decision

Implement a three-tier parser fallback chain in `api/services/ingestion.py` (`IngestorService`):

1. **Tier 1 — Docling** (primary): Full structural parsing producing titles, paragraphs, tables, images, and captions as typed elements. Preferred when available.
2. **Tier 2 — Unstructured** (secondary): Activated when Docling raises an exception or returns empty/null output. Produces structural elements with less granularity than Docling.
3. **Tier 3 — Raw text** (tertiary): PyPDF2 for PDF, python-docx for DOCX. Flat text extraction with no structural metadata. All chunks produced from this tier are stamped with `quality_flag: "raw_fallback"`.

Rules:
- Each tier transition is logged as a structured event (`parser_failed` WARNING + `fallback_triggered` WARNING) with `from_parser` and `to_parser` fields per SKILL-D.
- If all three tiers fail, the system performs a **hard-reject** with a structured error — no silent degradation.
- The `parser_used` field is recorded in the document manifest for audit and incremental rerun decisions.
- Each tier is individually toggleable via environment variables (`ENABLE_DOCLING`, `ENABLE_UNSTRUCTURED`, `ENABLE_RAW_FALLBACK` in `api/config.py`), all defaulting to `true`.

## Consequences

### Positive

- **Resilience**: Near-total ingestion coverage — only truly corrupt files are rejected. In testing, raw text fallback catches all cases that the structural parsers miss.
- **Quality preservation**: The cascade prioritizes structural richness. Documents parsed by Tier 1 produce higher-quality chunks with better heading boundaries and isolated tables, improving downstream extraction accuracy.
- **Transparency**: `parser_used` in the manifest and `raw_fallback` quality flags give curators clear visibility into parse quality. The curation UI surfaces these flags so reviewers know which chunks may need extra scrutiny.
- **Incremental reruns**: Manifest records `parser_config_hash` alongside `content_hash`. If parser toggles change, stale manifests are invalidated and documents are re-parsed with the new configuration.
- **Testability**: Each tier is independently testable. Fallback behavior is verified by mocking tier failures in `tests/unit/test_ingestion_fallback.py` (10 tests covering all tier combinations).

### Negative

- **Dependency weight**: Three parsing libraries (docling, unstructured, PyPDF2/python-docx) increase the dependency footprint and container image size.
- **Fallback complexity**: The chain adds branching logic and requires careful error handling at each transition. Each tier must catch exceptions without masking the original error context.
- **Quality gap**: Chunks from Tier 3 lack structural metadata (no heading boundaries, no table isolation), which may reduce extraction quality for those documents. The `raw_fallback` flag mitigates this by signaling reduced confidence to downstream agents.

## References

- [SPEC-03 S-03.1](../specs/SPEC-03-ingestion.md) — Parser fallback chain specification
- [SPEC-03 S-03.2](../specs/SPEC-03-ingestion.md) — Chunking with quality flags
- `api/services/ingestion.py` — IngestorService implementation
- `api/config.py` — Parser toggle environment variables
- `tests/unit/test_ingestion_fallback.py` — Fallback chain tests
