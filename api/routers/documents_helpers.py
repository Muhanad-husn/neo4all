"""
api/routers/documents_helpers.py — Adapter helpers for the documents router.

These functions bridge service layer types and provide aggregations that are
needed by the route handlers but are not business logic of any single service.
Extracted here per SKILL-B R-B7 (module ≤ ~400 lines).

Functions
---------
_compute_parser_config_hash  — Build the parser_config_hash for DocumentManifest.
_text_to_elements             — Adapt flat text → list[ParsedElement] for ChunkingService.
_compute_quality_summary      — Aggregate flag counts from Chunk list (ingest response).
_compute_quality_flags_summary — Aggregate flag counts from ChunkOut list (chunks response).
"""

from __future__ import annotations

import re
from typing import Any

from api.models.document import ChunkQualityFlag, derive_parser_config_hash
from api.routers.documents_models import ChunkOut, QualityFlagsSummary, QualitySummary
from api.services.chunking import ParsedElement, ParsedElementType

# ---------------------------------------------------------------------------
# Markdown structure detection regexes (used by _classify_element)
# ---------------------------------------------------------------------------
_RE_HEADING = re.compile(r"^#{1,6}\s+\S")
_RE_TABLE_SEP = re.compile(r"^\|?[\s\-:]+(\|[\s\-:]+)+\|?\s*$", re.MULTILINE)
_RE_LIST_ITEM = re.compile(r"^(\s*[-*+]\s+|\s*\d+[.)]\s+)")


def _compute_parser_config_hash() -> str:
    """Return the parser_config_hash matching IngestorService._build_parser_config().

    Reads the same three parser-toggle settings used internally by
    IngestorService so DocumentManifest.parser_config_hash will match on a
    future re-ingestion check.  Must stay in sync with that method.
    """
    from api.config import get_settings

    s = get_settings()
    return derive_parser_config_hash(
        {
            "enable_docling": s.ENABLE_DOCLING,
            "enable_unstructured": s.ENABLE_UNSTRUCTURED,
            "enable_raw_fallback": s.ENABLE_RAW_FALLBACK,
        }
    )


def _classify_element(text: str) -> ParsedElementType:
    """Classify a text block by its Markdown structure.

    Checks the first non-empty line against compiled regexes in priority
    order: heading > table > list_item > paragraph.  This is deterministic —
    same input always yields the same type.
    """
    first_line = ""
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    if not first_line:
        return ParsedElementType.paragraph

    if _RE_HEADING.match(first_line):
        return ParsedElementType.heading

    if _RE_TABLE_SEP.search(text):
        return ParsedElementType.table

    if _RE_LIST_ITEM.match(first_line):
        return ParsedElementType.list_item

    return ParsedElementType.paragraph


def _text_to_elements(text: str) -> list[ParsedElement]:
    """Convert extracted text to a ParsedElement list for ChunkingService.

    Splits on double newlines (the paragraph separator used by all three
    parser tiers) so ChunkingService receives multiple elements and
    size-based chunking can operate across paragraph boundaries.

    Each block is classified by its Markdown structure (heading, table,
    list_item, or paragraph) so that ChunkingService's heading-aware
    grouping logic can create proper chunk boundaries.

    All elements receive ``page=0``.  For raw_text output this satisfies the
    ChunkingService contract ("set page=0 explicitly on the raw_text path").
    For Docling/Unstructured markdown it is a conservative placeholder —
    structural page tracking is left to a future increment.

    Args:
        text: Extracted text from IngestResult.extracted_text.

    Returns:
        Non-empty list of classified ParsedElements; falls back to a single
        element wrapping the whole text when no double-newline separators
        are present.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        stripped = text.strip()
        paragraphs = [stripped] if stripped else []

    return [
        ParsedElement(
            element_type=_classify_element(p),
            text=p,
            page=0,
        )
        for p in paragraphs
    ]


def _compute_quality_summary(chunks: list[Any]) -> QualitySummary:
    """Count quality flags across Chunk objects from ChunkingService.

    Args:
        chunks: Chunk instances produced by ChunkingService.chunk_document().

    Returns:
        QualitySummary with per-flag counts and aggregate totals.
    """
    counts: dict[ChunkQualityFlag, int] = {
        ChunkQualityFlag.low_ocr_confidence: 0,
        ChunkQualityFlag.low_text_density: 0,
        ChunkQualityFlag.raw_fallback: 0,
    }
    flagged = 0
    for chunk in chunks:
        if chunk.quality_flags:
            flagged += 1
            for flag in chunk.quality_flags:
                if flag in counts:
                    counts[flag] += 1

    return QualitySummary(
        low_ocr_confidence=counts[ChunkQualityFlag.low_ocr_confidence],
        low_text_density=counts[ChunkQualityFlag.low_text_density],
        raw_fallback=counts[ChunkQualityFlag.raw_fallback],
        total_chunks=len(chunks),
        flagged_chunks=flagged,
    )


def _compute_quality_flags_summary(chunks: list[ChunkOut]) -> QualityFlagsSummary:
    """Count quality flags across ChunkOut records sourced from Qdrant payload.

    Args:
        chunks: ChunkOut instances built from Qdrant payload dicts.

    Returns:
        QualityFlagsSummary with per-flag counts for the UI summary panel.
    """
    return QualityFlagsSummary(
        low_ocr_confidence=sum(
            1 for c in chunks if "low_ocr_confidence" in c.quality_flags
        ),
        low_text_density=sum(
            1 for c in chunks if "low_text_density" in c.quality_flags
        ),
        raw_fallback=sum(
            1 for c in chunks if "raw_fallback" in c.quality_flags
        ),
    )
