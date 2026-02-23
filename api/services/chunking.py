"""
api/services/chunking.py — Semantic document chunking service (SPEC-03 S-03.2).

Converts an ordered list of ParsedElement objects into immutable, deterministically-
identified Chunk objects using a structure-respecting grouping algorithm.

Grouping rules (priority order):
  1. Table     → always standalone; never merged.
  2. Image     → standalone; immediately-following Caption is consumed into the group.
  3. Heading   → flushes accumulator, starts a fresh chunk seeded with the heading.
  4. Paragraph / list_item / orphan caption / other → accumulated until the next
                 element would exceed max_chars or max_tokens, then flushed.
  5. Header / Footer → silently skipped (page-level noise).

Soft quality flags (applied at flush, processing continues):
  low_ocr_confidence  — any element confidence < 0.7
  low_text_density    — chunk text < 20 chars

Hard-reject (ChunkingError raised, chunking_rejected ERROR logged):
  missing_page_info   — all elements in a flush group have page=None
  empty_chunk_text    — flush attempted with no non-whitespace text
  empty_document      — zero chunks produced from the full element list

chunk_id derivation is owned by the Chunk model (CLAUDE.md §5):
  chunk_id         = SHA-256(doc_id +"\\x00"+ start_page +"\\x00"+ chunk_index)
  start_page_locator = f"p{start_page}:c{chunk_index}"

Sensitive data: chunk text is NEVER logged (SKILL-D R-D5).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.models.document import Chunk, ChunkQualityFlag, Document
from api.observability.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
_DEFAULT_MAX_CHARS: int = 1000
_DEFAULT_MAX_TOKENS: int = 250
_CHARS_PER_TOKEN: int = 4         # 4 chars ≈ 1 token (conservative)
_MIN_OCR_CONFIDENCE: float = 0.7
_MIN_TEXT_DENSITY_CHARS: int = 20


# ---------------------------------------------------------------------------
# Typed input: structured parser element (SKILL-A R-A3)
# ---------------------------------------------------------------------------

class ParsedElementType(StrEnum):
    """Structural classification returned by a parser tier."""

    heading = "heading"       # Section heading — triggers chunk boundary
    paragraph = "paragraph"   # Body paragraph — accumulated
    table = "table"           # Tabular data — always standalone
    image = "image"           # Image — standalone; adjacent caption consumed
    caption = "caption"       # Figure/table caption — consumed by image or accumulated
    list_item = "list_item"   # List item — accumulated
    header = "header"         # Page header — skipped
    footer = "footer"         # Page footer — skipped
    other = "other"           # Any other element — accumulated


class ParsedElement(BaseModel):
    """Typed inter-service contract between IngestorService and ChunkingService.

    Raw dicts are banned at module boundaries (SKILL-A R-A3).

    Attributes:
        element_type: Structural classification of this element.
        text:         Extracted text; empty string for image-only elements.
        page:         0-indexed page number. Raw-text path must set this to 0.
        bbox:         Bounding box (x0, y0, x1, y1) in parser-native coords.
        confidence:   OCR confidence [0.0, 1.0]; None for native-text content.
        metadata:     Extensible parser-specific fields.
    """

    model_config = ConfigDict(frozen=True)

    element_type: ParsedElementType
    text: str = ""
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("page")
    @classmethod
    def _page_non_negative(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"page must be >= 0; got {v}")
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be in [0.0, 1.0]; got {v}")
        return v


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class ChunkingError(Exception):
    """Raised on hard-reject conditions during chunking (never silently swallowed).

    Attributes:
        code:    Machine-readable code ("missing_page_info", "empty_chunk_text",
                 "empty_document").
        message: Human-readable description.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Internal: mutable element accumulator
# ---------------------------------------------------------------------------

class _PendingChunk:
    """Mutable buffer for elements being grouped before a Chunk is emitted."""

    __slots__ = ("elements",)

    def __init__(self) -> None:
        self.elements: list[ParsedElement] = []

    @property
    def text(self) -> str:
        return "\n\n".join(e.text for e in self.elements if e.text.strip())

    def is_empty(self) -> bool:
        return not self.text.strip()

    def would_exceed_limit(self, candidate: ParsedElement, max_chars: int, max_tokens: int) -> bool:
        sep = "\n\n" if self.text else ""
        projected = len(self.text + sep + candidate.text)
        return projected > max_chars or (projected // _CHARS_PER_TOKEN) > max_tokens


# ---------------------------------------------------------------------------
# ChunkingService
# ---------------------------------------------------------------------------

class ChunkingService:
    """Segments parsed elements into semantically coherent, immutable Chunks.

    Stateless between calls; safe for concurrent use.

    Args:
        max_chars:  Max character count per accumulated chunk (default 1000).
        max_tokens: Max estimated token count per chunk (default 250).
    """

    def __init__(
        self,
        max_chars: int = _DEFAULT_MAX_CHARS,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> None:
        self._max_chars = max_chars
        self._max_tokens = max_tokens

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def chunk_document(
        self,
        document: Document,
        elements: list[ParsedElement],
        default_quality_flags: tuple[ChunkQualityFlag, ...] = (),
    ) -> list[Chunk]:
        """Convert an ordered ParsedElement list into a list of Chunks.

        Args:
            document:              Provides doc_id and run_id for every Chunk.
            elements:              Ordered elements from the winning parser tier.
                                   For raw_text, wrap text in a paragraph element
                                   with page=0 before calling.
            default_quality_flags: Flags inherited from IngestorService (e.g.
                                   raw_fallback). Applied to every Chunk produced.

        Returns:
            Non-empty list of Chunk objects in document order.

        Raises:
            ChunkingError: On any hard-reject condition.
        """
        chunks: list[Chunk] = []
        chunk_index: int = 0
        pending = _PendingChunk()

        i: int = 0
        while i < len(elements):
            element = elements[i]

            # ---- Table: always standalone ----
            if element.element_type == ParsedElementType.table:
                if not pending.is_empty():
                    chunks.append(self._flush(pending, chunk_index, document, default_quality_flags))
                    chunk_index += 1
                    pending = _PendingChunk()
                if element.text.strip():
                    tbl = _PendingChunk()
                    tbl.elements.append(element)
                    chunks.append(self._flush(tbl, chunk_index, document, default_quality_flags))
                    chunk_index += 1
                i += 1

            # ---- Image: standalone; consume adjacent caption ----
            elif element.element_type == ParsedElementType.image:
                if not pending.is_empty():
                    chunks.append(self._flush(pending, chunk_index, document, default_quality_flags))
                    chunk_index += 1
                    pending = _PendingChunk()
                img = _PendingChunk()
                img.elements.append(element)
                i += 1
                if i < len(elements) and elements[i].element_type == ParsedElementType.caption:
                    img.elements.append(elements[i])
                    i += 1
                if not img.is_empty():
                    chunks.append(self._flush(img, chunk_index, document, default_quality_flags))
                    chunk_index += 1

            # ---- Heading: boundary — flush then seed fresh accumulator ----
            elif element.element_type == ParsedElementType.heading:
                if not pending.is_empty():
                    chunks.append(self._flush(pending, chunk_index, document, default_quality_flags))
                    chunk_index += 1
                    pending = _PendingChunk()
                if element.text.strip():
                    pending.elements.append(element)
                i += 1

            # ---- Page header/footer: skip silently ----
            elif element.element_type in (ParsedElementType.header, ParsedElementType.footer):
                logger.debug(
                    "element_skipped",
                    doc_id=document.doc_id,
                    element_type=str(element.element_type),
                    page=element.page,
                )
                i += 1

            # ---- Paragraph / list_item / orphan caption / other: accumulate ----
            else:
                if element.text.strip():
                    if not pending.is_empty() and pending.would_exceed_limit(
                        element, self._max_chars, self._max_tokens
                    ):
                        chunks.append(self._flush(pending, chunk_index, document, default_quality_flags))
                        chunk_index += 1
                        pending = _PendingChunk()
                    pending.elements.append(element)
                i += 1

        # Flush any remaining accumulated content
        if not pending.is_empty():
            chunks.append(self._flush(pending, chunk_index, document, default_quality_flags))

        if not chunks:
            self._hard_reject(
                code="empty_document",
                message=(
                    f"Chunking produced zero chunks for doc_id={document.doc_id!r}. "
                    "The element list may be empty or contain only skippable elements."
                ),
                doc_id=document.doc_id,
            )

        logger.info(
            "chunking_complete",
            doc_id=document.doc_id,
            run_id=document.run_id,
            total_chunks=len(chunks),
            parser_used=document.parser_used,
        )

        return chunks

    # ------------------------------------------------------------------
    # Internal: materialise _PendingChunk → Chunk
    # ------------------------------------------------------------------

    def _flush(
        self,
        pending: _PendingChunk,
        chunk_index: int,
        document: Document,
        default_quality_flags: tuple[ChunkQualityFlag, ...],
    ) -> Chunk:
        """Convert pending buffer into an immutable Chunk; apply quality flags.

        Hard-rejects on empty text or unresolvable start_page.
        """
        text = pending.text

        # Hard-reject: empty text
        if not text.strip():
            self._hard_reject(
                code="empty_chunk_text",
                message=(
                    f"Flush attempted with no text at chunk_index={chunk_index} "
                    f"for doc_id={document.doc_id!r}."
                ),
                doc_id=document.doc_id,
            )

        # Hard-reject: missing page info → chunk_id cannot be computed
        pages = [e.page for e in pending.elements if e.page is not None]
        if not pages:
            self._hard_reject(
                code="missing_page_info",
                message=(
                    f"All {len(pending.elements)} element(s) in chunk_index="
                    f"{chunk_index} have page=None (doc_id={document.doc_id!r}). "
                    "Set page=0 explicitly on the raw_text path."
                ),
                doc_id=document.doc_id,
            )

        start_page: int = min(pages)  # type: ignore[type-var]
        end_page: int = max(pages)    # type: ignore[type-var]

        # Soft quality flags
        flags: set[ChunkQualityFlag] = set(default_quality_flags)

        confidences = [e.confidence for e in pending.elements if e.confidence is not None]
        if confidences and min(confidences) < _MIN_OCR_CONFIDENCE:
            flags.add(ChunkQualityFlag.low_ocr_confidence)
            logger.warning(
                "quality_flag_added",
                flag=str(ChunkQualityFlag.low_ocr_confidence),
                chunk_index=chunk_index,
                doc_id=document.doc_id,
                reason=f"min_confidence={min(confidences):.3f} < {_MIN_OCR_CONFIDENCE}",
            )

        if len(text.strip()) < _MIN_TEXT_DENSITY_CHARS:
            flags.add(ChunkQualityFlag.low_text_density)
            logger.warning(
                "quality_flag_added",
                flag=str(ChunkQualityFlag.low_text_density),
                chunk_index=chunk_index,
                doc_id=document.doc_id,
                reason=f"text_length={len(text.strip())} < {_MIN_TEXT_DENSITY_CHARS}",
            )

        # Metadata: element types, count, bboxes, OCR range
        bboxes = [list(e.bbox) for e in pending.elements if e.bbox is not None]
        chunk_meta: dict[str, Any] = {
            "element_types": sorted({str(e.element_type) for e in pending.elements}),
            "element_count": len(pending.elements),
        }
        if bboxes:
            chunk_meta["bboxes"] = bboxes
        if confidences:
            chunk_meta["min_ocr_confidence"] = round(min(confidences), 4)
            chunk_meta["max_ocr_confidence"] = round(max(confidences), 4)

        chunk = Chunk(
            doc_id=document.doc_id,
            run_id=document.run_id,
            text=text,
            start_page=start_page,
            end_page=end_page,
            chunk_index=chunk_index,
            quality_flags=tuple(sorted(flags)),
            metadata=chunk_meta,
        )

        logger.info(
            "chunk_created",
            chunk_id=chunk.chunk_id,
            chunk_index=chunk_index,
            doc_id=document.doc_id,
            start_page=start_page,
            end_page=end_page,
            char_count=len(text),
            quality_flags=[str(f) for f in chunk.quality_flags],
        )

        return chunk

    # ------------------------------------------------------------------
    # Internal: hard-reject helper
    # ------------------------------------------------------------------

    def _hard_reject(self, code: str, message: str, doc_id: str) -> NoReturn:
        """Log chunking_rejected at ERROR level and raise ChunkingError."""
        logger.error("chunking_rejected", code=code, doc_id=doc_id, reason=message)
        raise ChunkingError(code=code, message=message)


# ---------------------------------------------------------------------------
# Process-level singleton factory
# ---------------------------------------------------------------------------

_service_instance: ChunkingService | None = None


def get_chunking_service() -> ChunkingService:
    """Return the process-level ChunkingService singleton.

    Uses a module-level variable so tests can replace the instance without
    import-level side effects (same pattern as get_ingestor_service).
    """
    global _service_instance
    if _service_instance is None:
        _service_instance = ChunkingService()
    return _service_instance
