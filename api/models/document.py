"""
api/models/document.py — Document, Chunk, and DocumentManifest Pydantic models
(SPEC-03 S-03.3).

Defines the core ingestion domain types:

  - ChunkQualityFlag:   StrEnum of quality signals attached to individual chunks.
  - Document:           Immutable record of a single ingested source document.
  - Chunk:              Immutable, deterministically-identified text segment.
  - DocumentManifest:   Ingestion manifest enabling incremental reruns.

Identity contract (CLAUDE.md §5):
    doc_id   = SHA-256(run_id + "\\x00" + source_identity + "\\x00" + content_hash)
    chunk_id = SHA-256(doc_id + "\\x00" + start_page + "\\x00" + chunk_index)

Null-byte separators prevent boundary-collision attacks where a different split
of the same concatenated bytes would otherwise yield the same digest, e.g.:
    ("ab", "c")  vs  ("a", "bc")

doc_id and chunk_id are @computed_field properties — they are never accepted as
constructor input and are always re-derived from the model's stored fields.
Because the owning models are frozen, the computed values are stable for each
object's lifetime.

Module-level derivation helpers (used by api/services/ingestion.py):
    derive_content_hash(document_bytes)  →  SHA-256 hex of raw bytes.
    derive_parser_config_hash(config)    →  SHA-256 hex of canonical config JSON.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Quality flag vocabulary
# ---------------------------------------------------------------------------

class ChunkQualityFlag(StrEnum):
    """Quality signals attached to a Chunk at ingestion time.

    Flags are not mutually exclusive — a single chunk can carry multiple flags.

    Values:
        low_ocr_confidence:  OCR confidence score is below the configured
                             threshold.  Text may contain recognition errors.
        low_text_density:    Character-to-page-area ratio is below the configured
                             threshold.  Typically indicates a scanned image or
                             a near-blank page.
        raw_fallback:        Chunk was produced by the tertiary raw-text parser
                             (PyPDF2 / python-docx / plain-text decode) after
                             both Docling and Unstructured failed.  No
                             structural metadata is present; quality cannot be
                             assessed.
    """

    low_ocr_confidence = "low_ocr_confidence"
    low_text_density = "low_text_density"
    raw_fallback = "raw_fallback"


# ---------------------------------------------------------------------------
# Module-level derivation helpers
# ---------------------------------------------------------------------------

def derive_content_hash(document_bytes: bytes) -> str:
    """Return the SHA-256 hex digest of raw document bytes.

    This value becomes Document.content_hash and participates in doc_id
    derivation.  It must be computed once from the original bytes before any
    parser transformation so that the hash reflects the source content, not
    any intermediate representation.

    Args:
        document_bytes: Raw bytes of the source document (PDF, DOCX, etc.).

    Returns:
        64-character lowercase hex string.
    """
    return hashlib.sha256(document_bytes).hexdigest()


def derive_parser_config_hash(config: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of a canonical parser configuration dict.

    The hash changes whenever parser version, model weights, or threshold
    settings change.  DocumentManifest.parser_config_hash stores this value
    so that re-ingestion can detect when a cached manifest was produced under
    a different parser configuration.

    Args:
        config: Dict describing the active parser configuration, e.g.:
                {"parser": "docling", "version": "1.2.0", "dpi": 300}.

    Returns:
        64-character lowercase hex string.

    Notes:
        Determinism guarantees mirror _derive_version_hash in api/schema/models:
        - json.dumps with sort_keys=True eliminates dict-key order variance.
        - Compact separators and ensure_ascii=True guarantee byte-stable output.
    """
    serialized = json.dumps(
        config,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# SHA-256 hex validator — shared by Document and DocumentManifest
# ---------------------------------------------------------------------------

def _validate_sha256_hex(field_name: str, v: str) -> str:
    """Raise ValueError if v is not a 64-character lowercase hex string."""
    if len(v) != 64 or not all(c in "0123456789abcdef" for c in v):
        raise ValueError(
            f"{field_name} must be a 64-character lowercase SHA-256 hex digest; "
            f"got {v!r}"
        )
    return v


# ---------------------------------------------------------------------------
# Document — immutable record of an ingested source document
# ---------------------------------------------------------------------------

class Document(BaseModel):
    """Immutable record of a single ingested source document.

    doc_id is a @computed_field derived from (run_id, source_identity,
    content_hash).  It is never accepted as constructor input and is always
    re-derived at object creation time from the model's stored fields.

    Identity contract (CLAUDE.md §5):
        doc_id = SHA-256(run_id + "\\x00" + source_identity + "\\x00" + content_hash)

    Attributes:
        run_id:          Governed run this document belongs to.
        source_identity: Stable, content-independent identifier of the source —
                         typically a filename, S3 object key, or canonical URL.
                         Two uploads of the same file should produce the same
                         source_identity so that re-ingestion is detectable.
        content_hash:    SHA-256 hex of the raw document bytes, computed by
                         derive_content_hash() before any parser transforms.
        parser_used:     Parser tier that succeeded: "docling", "unstructured",
                         or "raw_text".
        created_at:      UTC timestamp set by the ingestion service at the
                         moment the Document record is created.
        doc_id:          Deterministic 64-char hex digest (computed, read-only).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    source_identity: str
    content_hash: str
    parser_used: str
    created_at: datetime

    @field_validator("run_id", "source_identity", "parser_used")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v

    @field_validator("content_hash")
    @classmethod
    def _valid_content_hash(cls, v: str) -> str:
        return _validate_sha256_hex("content_hash", v)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def doc_id(self) -> str:
        """SHA-256 hex digest of (run_id, source_identity, content_hash).

        Null-byte separators prevent boundary collisions.
        Always re-derived from stored fields; never accepted as constructor input.
        """
        raw = (
            f"{self.run_id}\x00{self.source_identity}\x00{self.content_hash}"
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Chunk — immutable, deterministically-identified text segment
# ---------------------------------------------------------------------------

class Chunk(BaseModel):
    """Immutable text segment produced by the chunking service.

    chunk_id and start_page_locator are @computed_field properties derived from
    (doc_id, start_page, chunk_index).  They are never accepted as constructor
    input.

    Identity contract (CLAUDE.md §5):
        chunk_id = SHA-256(doc_id + "\\x00" + start_page + "\\x00" + chunk_index)

    start_page_locator is the canonical string representation of the chunk's
    position within the document.  It is stored in Qdrant metadata (SPEC-03
    S-03.5) so that chunks can be located by position.

    Attributes:
        doc_id:             64-char hex digest of the parent Document.
        run_id:             Governed run this chunk belongs to.
        text:               Extracted chunk text.  Never logged — log chunk_id
                            instead (SKILL-D R-D5).
        start_page:         0-indexed page number where the chunk begins.
        end_page:           0-indexed page number where the chunk ends.
                            Must be >= start_page.
        chunk_index:        0-indexed sequential position of this chunk within
                            the document.  Together with start_page it uniquely
                            identifies the chunk's position and participates in
                            chunk_id derivation.
        quality_flags:      Immutable tuple of ChunkQualityFlag values.
                            Empty tuple = no quality concerns detected.
        metadata:           Extensible dict for parser-specific data: bounding
                            boxes, OCR confidence scores, structural element
                            types, etc.  The model attribute itself cannot be
                            reassigned (frozen=True), but dict contents are
                            mutable by value — treat as read-only after creation.
        chunk_id:           Deterministic 64-char hex digest (computed, read-only).
        start_page_locator: Canonical position string (computed, read-only).
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    run_id: str
    text: str
    start_page: int
    end_page: int
    chunk_index: int
    quality_flags: tuple[ChunkQualityFlag, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("doc_id", "run_id")
    @classmethod
    def _non_empty_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v

    @field_validator("text")
    @classmethod
    def _text_non_empty(cls, v: str) -> str:
        # SPEC-03 S-03.2: hard-reject on missing content
        if not v.strip():
            raise ValueError("chunk text must be non-empty")
        return v

    @field_validator("start_page", "chunk_index")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be >= 0")
        return v

    @field_validator("end_page")
    @classmethod
    def _end_page_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("end_page must be >= 0")
        return v

    @model_validator(mode="after")
    def _end_page_gte_start_page(self) -> "Chunk":
        if self.end_page < self.start_page:
            raise ValueError(
                f"end_page ({self.end_page}) must be >= start_page ({self.start_page})"
            )
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def start_page_locator(self) -> str:
        """Canonical position string stored in Qdrant metadata and log events.

        Format: "p{start_page}:c{chunk_index}" — human-readable, stable, and
        unique within a document for the lifetime of a run.
        """
        return f"p{self.start_page}:c{self.chunk_index}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def chunk_id(self) -> str:
        """SHA-256 hex digest of (doc_id, start_page, chunk_index).

        Null-byte separators prevent boundary collisions.
        Always re-derived from stored fields; never accepted as constructor input.
        """
        raw = (
            f"{self.doc_id}\x00{self.start_page}\x00{self.chunk_index}"
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# DocumentManifest — ingestion manifest for incremental reruns
# ---------------------------------------------------------------------------

class DocumentManifest(BaseModel):
    """Ingestion manifest for a single document within a run.

    Stored in S3 via api/storage/artifacts.py and cached in Redis with key
    CacheKey.manifest(run_id, doc_id) at a 1-hour TTL (SKILL-D R-D8).
    Invalidated on re-ingestion of the same document.

    A stored manifest enables:
    - Incremental reruns: skip documents whose content_hash has not changed
      and whose parser_config_hash matches the currently active configuration.
    - Parser change detection: re-ingest if parser_config_hash differs from
      the current active configuration hash.
    - Chunk ID reconstruction: rebuild chunk references without re-parsing.

    Attributes:
        doc_id:             64-char hex digest linking to Document.doc_id.
        run_id:             Governed run this manifest belongs to.
        content_hash:       SHA-256 hex of the raw document bytes at ingestion
                            time.  Matches Document.content_hash.
        parser_config_hash: SHA-256 of the canonical parser configuration dict
                            at ingestion time (derived via derive_parser_config_hash).
                            Changes when parser version, model weights, or
                            thresholds change.
        chunk_ids:          Ordered tuple of chunk_id values produced from this
                            document, in ascending chunk_index order.  At least
                            one chunk_id is required.
        timestamp:          UTC time at which the manifest was finalised by the
                            ingestion service.
    """

    model_config = ConfigDict(frozen=True)

    doc_id: str
    run_id: str
    content_hash: str
    parser_config_hash: str
    chunk_ids: tuple[str, ...]
    timestamp: datetime
    parser_used: str = ""
    source_identity: str = ""

    @field_validator("doc_id", "run_id")
    @classmethod
    def _non_empty_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v

    @field_validator("content_hash")
    @classmethod
    def _valid_content_hash(cls, v: str) -> str:
        return _validate_sha256_hex("content_hash", v)

    @field_validator("parser_config_hash")
    @classmethod
    def _valid_parser_config_hash(cls, v: str) -> str:
        return _validate_sha256_hex("parser_config_hash", v)

    @field_validator("chunk_ids")
    @classmethod
    def _at_least_one_chunk(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # SPEC-03 S-03.2: a manifest with zero chunks is a hard-reject condition
        if len(v) == 0:
            raise ValueError("chunk_ids must contain at least one chunk_id")
        return v
