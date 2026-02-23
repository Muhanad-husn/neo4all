"""
tests/unit/test_chunking.py — ChunkingService semantic grouping and quality flags
(SPEC-03 file 12).

Acceptance criteria (CLAUDE.md §4.3/§4.4, SPEC-03 S-03.2):
  - Heading elements flush the accumulator and create a new chunk boundary.
  - Table elements are always standalone chunks (never merged with neighbours).
  - Accumulated chunks are split when a new element would exceed max_chars or
    max_tokens — whichever limit is hit first.
  - Elements with OCR confidence < 0.7 cause the containing chunk to receive
    ChunkQualityFlag.low_ocr_confidence (soft flag; processing continues).
  - Chunks whose stripped text is < 20 chars receive ChunkQualityFlag.low_text_density
    (soft flag; processing continues).
  - A group of elements all with page=None triggers a hard-reject:
    ChunkingError is raised with code='missing_page_info'.

Quality threshold constants mirror api/services/chunking.py module-level values.
No network, no LLM, no Neo4j, no file I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from api.models.document import ChunkQualityFlag, Document, derive_content_hash
from api.services.chunking import (
    ChunkingError,
    ChunkingService,
    ParsedElement,
    ParsedElementType,
)

# ---------------------------------------------------------------------------
# Quality threshold constants (mirror api/services/chunking.py)
# ---------------------------------------------------------------------------

# Chunk receives low_ocr_confidence if any element confidence < this value.
_MIN_OCR_CONFIDENCE: float = 0.7

# Chunk receives low_text_density if stripped text length < this value.
_MIN_TEXT_DENSITY_CHARS: int = 20

# ---------------------------------------------------------------------------
# Shared document fixture
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-chunking-00001"
_SOURCE_IDENTITY = "reports/test_document.pdf"
_CREATED_AT = datetime(2026, 2, 23, 12, 0, 0, tzinfo=UTC)
_CONTENT_HASH = derive_content_hash(b"chunking-unit-test-payload")


def _make_document(parser_used: str = "docling") -> Document:
    """Minimal Document — provides doc_id and run_id to ChunkingService."""
    return Document(
        run_id=_RUN_ID,
        source_identity=_SOURCE_IDENTITY,
        content_hash=_CONTENT_HASH,
        parser_used=parser_used,
        created_at=_CREATED_AT,
    )


# ---------------------------------------------------------------------------
# Element builder helpers
# ---------------------------------------------------------------------------


def _para(
    text: str,
    *,
    page: int | None = 0,
    confidence: float | None = None,
) -> ParsedElement:
    """Build a paragraph ParsedElement."""
    return ParsedElement(
        element_type=ParsedElementType.paragraph,
        text=text,
        page=page,
        confidence=confidence,
    )


def _heading(text: str, *, page: int = 0) -> ParsedElement:
    """Build a heading ParsedElement (triggers chunk boundary)."""
    return ParsedElement(
        element_type=ParsedElementType.heading,
        text=text,
        page=page,
    )


def _table(text: str, *, page: int = 0) -> ParsedElement:
    """Build a table ParsedElement (always standalone)."""
    return ParsedElement(
        element_type=ParsedElementType.table,
        text=text,
        page=page,
    )


# ---------------------------------------------------------------------------
# Tests: structural grouping
# ---------------------------------------------------------------------------


def test_semantic_grouping() -> None:
    """Heading flushes the accumulator and seeds the next chunk.

    Input:  [paragraph, heading, paragraph]
    Layout: para_intro | heading (flushes intro) → chunk 0 = intro paragraph
                                                    chunk 1 = heading + body paragraph

    The heading element is consumed into pending after the flush, so the next
    paragraph accumulates into the same chunk as the heading text.
    """
    svc = ChunkingService()
    doc = _make_document()

    elements = [
        _para("Introductory paragraph with sufficient text content here.", page=0),
        _heading("Section 1: Main Topic", page=1),
        _para("Body paragraph that follows the heading with enough content.", page=1),
    ]

    chunks = svc.chunk_document(doc, elements)

    assert len(chunks) == 2

    # chunk 0: intro paragraph only — heading flushed it before the boundary
    assert "Introductory paragraph" in chunks[0].text
    assert "Section 1" not in chunks[0].text

    # chunk 1: heading text + body paragraph (joined by double newline)
    assert "Section 1" in chunks[1].text
    assert "Body paragraph" in chunks[1].text

    # chunk_index is assigned in ascending order
    assert chunks[0].chunk_index == 0
    assert chunks[1].chunk_index == 1

    # doc_id and run_id are inherited from the Document
    for chunk in chunks:
        assert chunk.doc_id == doc.doc_id
        assert chunk.run_id == doc.run_id


def test_table_isolation() -> None:
    """Table element is always a standalone chunk; it never merges with neighbours.

    Input:  [paragraph, table, paragraph]
    Result: 3 chunks — one per element, each at a separate chunk_index.

    The table flushes any pending accumulator before producing its own chunk.
    The trailing paragraph is accumulated into a new buffer after the table.
    """
    svc = ChunkingService()
    doc = _make_document()

    elements = [
        _para("Text before the table with sufficient content for a chunk.", page=0),
        _table("| Column A | Column B |\n|----------|----------|\n| Value 1  | Value 2  |", page=1),
        _para("Text after the table with sufficient content for a chunk.", page=2),
    ]

    chunks = svc.chunk_document(doc, elements)

    assert len(chunks) == 3

    # Correct content in each chunk
    assert "Text before" in chunks[0].text
    assert "Column A" in chunks[1].text   # table is isolated as chunk 1
    assert "Text after" in chunks[2].text

    # Table is exactly chunk_index 1
    assert chunks[1].chunk_index == 1

    # Page boundaries are respected
    assert chunks[0].start_page == 0
    assert chunks[1].start_page == 1
    assert chunks[2].start_page == 2


def test_size_limits() -> None:
    """Accumulated paragraphs are split when the next element would exceed max_chars.

    Use max_chars=60 so that two ~55-char paragraphs cannot be merged:
        projected = len(para_a) + len('\\n\\n') + len(para_b) = 53 + 2 + 55 = 110 > 60

    The first paragraph is flushed before the second is accumulated so that
    each chunk stays within the configured limit.
    """
    svc = ChunkingService(max_chars=60, max_tokens=500)
    doc = _make_document()

    # 53 chars — fits within 60 alone
    para_a = _para("First paragraph content that fits within sixty chars.", page=0)
    # 55 chars — would make projected 110 chars with separator
    para_b = _para("Second paragraph content that would overflow the limit.", page=0)

    chunks = svc.chunk_document(doc, [para_a, para_b])

    assert len(chunks) == 2
    assert "First paragraph" in chunks[0].text
    assert "Second paragraph" in chunks[1].text
    assert chunks[0].chunk_index == 0
    assert chunks[1].chunk_index == 1


# ---------------------------------------------------------------------------
# Tests: soft quality flags
# ---------------------------------------------------------------------------


def test_quality_flags_ocr() -> None:
    """Element with OCR confidence below threshold receives low_ocr_confidence flag.

    Threshold: confidence < 0.7 (mirrored from _MIN_OCR_CONFIDENCE).
    Flag is soft — processing continues and the chunk is returned.
    Chunk also carries the metadata field 'min_ocr_confidence'.
    """
    svc = ChunkingService()
    doc = _make_document()

    # confidence=0.65 < _MIN_OCR_CONFIDENCE (0.7)
    elements = [
        _para(
            "This is a scanned page with poor OCR quality output text here.",
            page=0,
            confidence=0.65,
        ),
    ]

    chunks = svc.chunk_document(doc, elements)

    assert len(chunks) == 1
    assert ChunkQualityFlag.low_ocr_confidence in chunks[0].quality_flags
    # OCR stats are recorded in chunk metadata
    assert "min_ocr_confidence" in chunks[0].metadata
    assert chunks[0].metadata["min_ocr_confidence"] == pytest.approx(0.65, abs=1e-4)


def test_quality_flags_ocr_at_boundary() -> None:
    """Confidence exactly at the threshold (0.7) does NOT receive the flag.

    The condition is strictly less-than: confidence < 0.7.
    An element at exactly 0.7 is borderline-acceptable; no flag is added.
    """
    svc = ChunkingService()
    doc = _make_document()

    elements = [
        _para(
            "This page meets the minimum OCR confidence threshold exactly.",
            page=0,
            confidence=_MIN_OCR_CONFIDENCE,  # exactly 0.7 — not flagged
        ),
    ]

    chunks = svc.chunk_document(doc, elements)

    assert len(chunks) == 1
    assert ChunkQualityFlag.low_ocr_confidence not in chunks[0].quality_flags


def test_quality_flags_density() -> None:
    """Chunk text shorter than the minimum character count receives low_text_density.

    Threshold: stripped text length < 20 chars (mirrored from _MIN_TEXT_DENSITY_CHARS).
    Flag is soft — processing continues and the chunk is returned.
    """
    svc = ChunkingService()
    doc = _make_document()

    # "Short chunk." = 12 chars (< _MIN_TEXT_DENSITY_CHARS = 20)
    elements = [
        ParsedElement(
            element_type=ParsedElementType.paragraph,
            text="Short chunk.",
            page=0,
        ),
    ]

    chunks = svc.chunk_document(doc, elements)

    assert len(chunks) == 1
    assert ChunkQualityFlag.low_text_density in chunks[0].quality_flags


def test_quality_flags_both_may_coexist() -> None:
    """A chunk can carry both low_ocr_confidence and low_text_density simultaneously.

    If OCR confidence is low AND the extracted text is sparse, both flags are
    added — they are not mutually exclusive.
    """
    svc = ChunkingService()
    doc = _make_document()

    # Short AND low confidence
    elements = [
        ParsedElement(
            element_type=ParsedElementType.paragraph,
            text="Bad OCR pg.",   # 11 chars (< 20) AND confidence 0.50 (< 0.70)
            page=0,
            confidence=0.50,
        ),
    ]

    chunks = svc.chunk_document(doc, elements)

    assert len(chunks) == 1
    assert ChunkQualityFlag.low_ocr_confidence in chunks[0].quality_flags
    assert ChunkQualityFlag.low_text_density in chunks[0].quality_flags


# ---------------------------------------------------------------------------
# Tests: hard-reject conditions
# ---------------------------------------------------------------------------


def test_hard_reject_missing_id() -> None:
    """All elements with page=None in a flush group raises ChunkingError.

    chunk_id derivation requires a valid start_page.  When every element in
    the pending buffer has page=None, there is no usable page number.
    The service raises ChunkingError with code='missing_page_info' (never
    silently coerces or substitutes a default page number).
    """
    svc = ChunkingService()
    doc = _make_document()

    elements = [
        ParsedElement(
            element_type=ParsedElementType.paragraph,
            text="This paragraph has no page number set in the parser output.",
            page=None,  # Triggers hard-reject
        ),
    ]

    with pytest.raises(ChunkingError) as exc_info:
        svc.chunk_document(doc, elements)

    assert exc_info.value.code == "missing_page_info"


def test_hard_reject_empty_document() -> None:
    """An element list that produces zero chunks raises ChunkingError.

    If all elements are skippable (header/footer or empty text), the loop
    completes with no chunks produced.  The service raises ChunkingError
    with code='empty_document' rather than returning an empty list.
    """
    svc = ChunkingService()
    doc = _make_document()

    elements = [
        ParsedElement(element_type=ParsedElementType.header, text="Page 1", page=0),
        ParsedElement(element_type=ParsedElementType.footer, text="© 2026", page=0),
    ]

    with pytest.raises(ChunkingError) as exc_info:
        svc.chunk_document(doc, elements)

    assert exc_info.value.code == "empty_document"
