"""
tests/unit/test_doc_id.py — Deterministic doc_id derivation (SPEC-03 file 10).

Acceptance criteria (CLAUDE.md §5, SPEC-03 S-03.3):
  - doc_id is deterministic: identical (run_id, source_identity, content_hash)
    always produces the same 64-char SHA-256 hex digest.
  - doc_id changes when run_id, source_identity, or content bytes change.
  - derive_content_hash() uses SHA-256; output is stable across calls.
  - Identity contract:
      doc_id = SHA-256(run_id + NUL + source_identity + NUL + content_hash)

No network, no LLM, no Neo4j.
fixtures/sample.pdf is read as raw bytes to validate derive_content_hash.
"""

from __future__ import annotations

import hashlib
import pathlib
from datetime import UTC, datetime

from api.models.document import Document, derive_content_hash

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_FIXTURES_DIR = pathlib.Path(__file__).parent.parent.parent / "fixtures"

_RUN_ID = "unit-test-run-doc-id-0000000001"
_SOURCE_IDENTITY = "reports/annual_report_2025.pdf"
_CONTENT_BYTES = b"This is a deterministic test document payload."
_CREATED_AT = datetime(2026, 2, 23, 12, 0, 0, tzinfo=UTC)

# Pre-compute content_hash once; reused across multiple test builders so tests
# remain independent of the exact bytes-to-hex mapping they are verifying.
_CONTENT_HASH = derive_content_hash(_CONTENT_BYTES)


# ---------------------------------------------------------------------------
# Reference implementation
#
# Mirrors the identity contract in Document.doc_id but written independently
# to cross-validate that production code implements the correct formula.
# ---------------------------------------------------------------------------


def _reference_doc_id(run_id: str, source_identity: str, content_hash: str) -> str:
    """Canonical formula: SHA-256(run_id + NUL + source_identity + NUL + content_hash)."""
    raw = f"{run_id}\x00{source_identity}\x00{content_hash}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_document(
    *,
    run_id: str = _RUN_ID,
    source_identity: str = _SOURCE_IDENTITY,
    content_bytes: bytes = _CONTENT_BYTES,
    parser_used: str = "docling",
) -> Document:
    """Construct a Document from raw bytes — mirrors the ingestion service path."""
    return Document(
        run_id=run_id,
        source_identity=source_identity,
        content_hash=derive_content_hash(content_bytes),
        parser_used=parser_used,
        created_at=_CREATED_AT,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_doc_id_deterministic() -> None:
    """Same run_id + source_identity + content → same doc_id on every call.

    This is the foundational idempotency guarantee: re-ingesting the same
    document in the same run always produces the same doc_id so the manifest
    can detect that the document is already indexed (SPEC-03 S-03.4).
    Also validates that the output is a properly formatted 64-char lowercase
    SHA-256 hex digest (CLAUDE.md §5).
    """
    doc_a = _make_document()
    doc_b = _make_document()

    assert doc_a.doc_id == doc_b.doc_id
    assert len(doc_a.doc_id) == 64
    assert doc_a.doc_id == doc_a.doc_id.lower()
    assert all(c in "0123456789abcdef" for c in doc_a.doc_id)


def test_doc_id_matches_reference_implementation() -> None:
    """doc_id matches the independently written reference formula.

    Cross-validates that Document.doc_id encodes the exact identity contract
    from CLAUDE.md §5:
        SHA-256(run_id + NUL + source_identity + NUL + content_hash)
    """
    doc = _make_document()
    expected = _reference_doc_id(doc.run_id, doc.source_identity, doc.content_hash)

    assert doc.doc_id == expected


def test_doc_id_different_content() -> None:
    """Different document bytes → different doc_id, holding run and source constant.

    Ensures the content_hash component participates in doc_id derivation so
    that two versions of the same filename are tracked as distinct documents.
    """
    doc_v1 = _make_document(content_bytes=b"Version one of this document.")
    doc_v2 = _make_document(content_bytes=b"Version two of this document.")

    assert doc_v1.doc_id != doc_v2.doc_id


def test_doc_id_different_run() -> None:
    """Different run_id → different doc_id, even with identical content and source.

    Ensures the run_id component participates in doc_id derivation so that
    the same document uploaded into two separate runs is tracked independently.
    """
    doc_run_a = _make_document(run_id="run-aaa-0000000000000001")
    doc_run_b = _make_document(run_id="run-bbb-0000000000000002")

    assert doc_run_a.doc_id != doc_run_b.doc_id


def test_content_hash_sha256() -> None:
    """derive_content_hash() computes a SHA-256 hex digest of the input bytes.

    Loads fixtures/sample.pdf as raw bytes and validates:
      1. Output is a 64-char lowercase hex string.
      2. Output matches hashlib.sha256(bytes).hexdigest() exactly (SHA-256).
      3. Repeated calls with the same bytes return the same digest.

    Using a real fixture file exercises the helper on non-trivial binary
    content rather than a synthetic stub.
    """
    pdf_bytes = (_FIXTURES_DIR / "sample.pdf").read_bytes()

    result = derive_content_hash(pdf_bytes)
    expected = hashlib.sha256(pdf_bytes).hexdigest()

    assert result == expected, "derive_content_hash must use SHA-256"
    assert len(result) == 64
    assert result == result.lower()
    assert all(c in "0123456789abcdef" for c in result)
    # Idempotency: same bytes → same digest on every invocation
    assert derive_content_hash(pdf_bytes) == result


def test_doc_id_null_byte_separator_prevents_boundary_collision() -> None:
    """Null-byte separator prevents boundary-collision between split inputs.

    Without the separator the byte sequences for two different (run_id,
    source_identity) pairs could be identical:
        ("ab", "c")  concatenates to  b"abc"
        ("a",  "bc") concatenates to  b"abc"

    The null-byte separator makes these inputs distinct so they produce
    different digests (CLAUDE.md §5 identity contract, Document.doc_id docstring).
    """
    # Two documents with different field splits but identical naive concatenation
    doc_ab_c = Document(
        run_id="ab",
        source_identity="c",
        content_hash=_CONTENT_HASH,
        parser_used="docling",
        created_at=_CREATED_AT,
    )
    doc_a_bc = Document(
        run_id="a",
        source_identity="bc",
        content_hash=_CONTENT_HASH,
        parser_used="docling",
        created_at=_CREATED_AT,
    )

    assert doc_ab_c.doc_id != doc_a_bc.doc_id, (
        "Boundary collision detected: null-byte separator is not working in doc_id"
    )
