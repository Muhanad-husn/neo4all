"""api/services/ingestion_parsers.py — Three-tier parser implementations.

Extracted from api/services/ingestion.py (SKILL-B R-B7, >400-line split).
These are the three-tier parser implementations for the IngestorService:

  - _derive_doc_id:          Deterministic doc_id derivation (SHA-256).
  - _detect_file_type:       Extension + magic-byte file type detection.
  - _parse_with_docling:     Tier 1 — Docling full-fidelity Markdown export.
  - _parse_with_unstructured: Tier 2 — Unstructured auto-partition.
  - _parse_with_raw_text:    Tier 3 — PyPDF2 / python-docx / plain text.

All functions are synchronous and stateless.  IngestorService runs them
via asyncio.to_thread() in the fallback chain.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Internal: deterministic doc_id derivation
# (Mirrors Document.doc_id computed field — used before constructing Document
#  so the cache key can be built from content_hash alone.)
# ---------------------------------------------------------------------------

def _derive_doc_id(run_id: str, source_identity: str, content_hash: str) -> str:
    """Compute doc_id without constructing a Document instance.

    Replicates Document.doc_id (CLAUDE.md §5):
        SHA-256(run_id + NUL + source_identity + NUL + content_hash)

    Null-byte separators prevent boundary-collision attacks (see document.py).
    Called once per ingest_document() invocation, before any cache or S3 I/O.
    """
    raw = f"{run_id}\x00{source_identity}\x00{content_hash}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Format category constants
# ---------------------------------------------------------------------------

_TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".md", ".rst", ".csv", ".tsv",
    ".html", ".htm", ".xml", ".json", ".log",
})

_ZIP_OFFICE_EXTENSIONS: frozenset[str] = frozenset({
    ".docx", ".pptx", ".xlsx", ".odt", ".epub",
})


# ---------------------------------------------------------------------------
# Internal: file type detection
# ---------------------------------------------------------------------------

def _detect_file_type(source_identity: str, file_bytes: bytes) -> str:
    """Return ``'pdf'``, ``'docx'``, ``'text'``, or ``'unknown'``.

    Detection priority:
      1. Filename extension (fast path).
      2. Magic-byte heuristic for extensionless or ambiguous files.

    Extension mapping:
      - ``.pdf``                               → ``'pdf'``
      - ``.docx``                              → ``'docx'``
      - ``.txt .md .rst .csv .tsv .html .htm
         .xml .json .log``                     → ``'text'``
      - ``.pptx .xlsx .odt .epub`` (and other
        ZIP-based Office formats)              → ``'unknown'``
      - Any other recognised extension         → ``'unknown'``

    Magic-byte fallback (no matching extension):
      - ``%PDF``                               → ``'pdf'``
      - ``PK\\x03\\x04``  (ZIP header)         → ``'docx'``
      - Valid UTF-8 content                    → ``'text'``
      - Anything else                          → ``'unknown'``
    """
    suffix = Path(source_identity).suffix.lower()

    # Extension-based fast path
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix in _TEXT_EXTENSIONS:
        return "text"
    if suffix in _ZIP_OFFICE_EXTENSIONS:
        # Non-DOCX ZIP-based formats — Tier 1/2 may handle them,
        # but raw-text fallback cannot.
        return "unknown"
    if suffix:
        # Known extension but not in our categories
        return "unknown"

    # No recognised extension — fall back to magic bytes
    if file_bytes[:4] == b"%PDF":
        return "pdf"
    if file_bytes[:4] == b"PK\x03\x04":
        # ZIP-based Office format — best-effort guess is DOCX
        return "docx"

    # UTF-8 heuristic for extensionless files
    try:
        file_bytes.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        pass

    return "unknown"


# ---------------------------------------------------------------------------
# Internal: parser tier implementations (synchronous — run via to_thread)
# ---------------------------------------------------------------------------

def _parse_with_docling(file_bytes: bytes, source_identity: str) -> str:
    """Tier 1: parse with Docling and export full-fidelity Markdown.

    Prefers an in-memory DocumentStream (Docling 2.x) to avoid any filesystem
    I/O.  If the DocumentStream import fails (older Docling version), falls
    back to a secure temp file that is deleted in the finally block.

    Args:
        file_bytes:      Raw document bytes.  Not logged (SKILL-D R-D5).
        source_identity: Used as the stream name for Docling metadata.

    Returns:
        Non-empty Markdown string.

    Raises:
        Exception: ImportError if Docling is not installed; any Docling
                   conversion error; ValueError if output is empty.
    """
    from docling.document_converter import DocumentConverter  # type: ignore[import-untyped]

    result: Any

    try:
        # Docling 2.x in-memory path — no filesystem I/O.
        from docling.datamodel.base_models import DocumentStream  # type: ignore[import-untyped]

        source: Any = DocumentStream(name=source_identity, stream=BytesIO(file_bytes))
        result = DocumentConverter().convert(source)

    except ImportError:
        # Older Docling — write to a temp file.  Use mkstemp for security
        # and close the fd via fdopen before Docling opens the path on Windows.
        suffix = Path(source_identity).suffix.lower() or ".tmp"
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(tmp_fd, "wb") as fh:
                fh.write(file_bytes)
            # fd is now closed; Docling can open the path on Windows.
            result = DocumentConverter().convert(tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    text: str = result.document.export_to_markdown()
    if not text.strip():
        raise ValueError("docling produced empty output")
    return text


def _parse_with_unstructured(file_bytes: bytes, source_identity: str) -> str:
    """Tier 2: parse with Unstructured via auto-partition.

    Accepts BytesIO directly; no filesystem I/O required.

    Args:
        file_bytes:      Raw document bytes.  Not logged (SKILL-D R-D5).
        source_identity: Passed as metadata_filename for element annotation.

    Returns:
        Non-empty text string with double-newline paragraph separation.

    Raises:
        Exception: ImportError if Unstructured is not installed; any
                   partition error; ValueError if output is empty.
    """
    from unstructured.partition.auto import partition  # type: ignore[import-untyped]

    elements = partition(
        file=BytesIO(file_bytes),
        metadata_filename=source_identity,
    )
    text = "\n\n".join(str(e) for e in elements if str(e).strip())
    if not text.strip():
        raise ValueError("unstructured produced empty output")
    return text


def _parse_with_raw_text(file_bytes: bytes, source_identity: str) -> str:
    """Tier 3: extract flat text with PyPDF2 (PDF), python-docx (DOCX), or
    direct decode (plain-text formats).

    This tier produces no structural metadata.  Every chunk derived from
    its output must carry ChunkQualityFlag.raw_fallback — that flag is set
    by IngestorService, not here.

    Args:
        file_bytes:      Raw document bytes.  Not logged (SKILL-D R-D5).
        source_identity: Used only for file-type detection and error messages.

    Returns:
        Non-empty flat text string.

    Raises:
        ValueError:  Unsupported file type, or extraction produced empty text.
        Exception:   ImportError if the required library is not installed;
                     any library-level parse error.
    """
    file_type = _detect_file_type(source_identity, file_bytes)

    if file_type == "pdf":
        import PyPDF2  # type: ignore[import-untyped]

        reader = PyPDF2.PdfReader(BytesIO(file_bytes))
        pages = [
            reader.pages[i].extract_text() or ""
            for i in range(len(reader.pages))
        ]
        text = "\n\n".join(p for p in pages if p.strip())

    elif file_type == "docx":
        import docx  # type: ignore[import-untyped]

        doc = docx.Document(BytesIO(file_bytes))
        text = "\n\n".join(
            para.text for para in doc.paragraphs if para.text.strip()
        )

    elif file_type == "text":
        try:
            text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = file_bytes.decode("latin-1")

    else:
        raise ValueError(
            f"unsupported file type for raw fallback: {source_identity!r}. "
            "Expected a PDF, DOCX, or text-based file."
        )

    if not text.strip():
        raise ValueError(
            f"raw text extraction produced empty output for {file_type!r} file"
        )
    return text
