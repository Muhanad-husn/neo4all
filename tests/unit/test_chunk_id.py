"""
tests/unit/test_chunk_id.py — Deterministic chunk_id derivation (SPEC-03 file 11).

Acceptance criteria (CLAUDE.md §5, SPEC-03 S-03.2):
  - chunk_id is deterministic: identical (doc_id, start_page, chunk_index)
    always produces the same 64-char SHA-256 hex digest.
  - Changing start_page or chunk_index changes the chunk_id.
  - Re-creating the same chunks produces the same chunk_ids (idempotency).
  - Collecting chunk_ids in chunk_index order yields a stable, unique sequence.
  - Identity contract:
      chunk_id = SHA-256(doc_id + NUL + start_page + NUL + chunk_index)

No network, no LLM, no Neo4j, no document parsing.
"""

from __future__ import annotations

import hashlib

from api.models.document import Chunk, ChunkQualityFlag

# ---------------------------------------------------------------------------
# Shared constants
#
# _DOC_ID is a real SHA-256 hex digest (not a synthetic stub) to reflect the
# values that Document.doc_id produces in the ingestion pipeline.
# ---------------------------------------------------------------------------

_DOC_ID = hashlib.sha256(b"test-doc-chunk-id-unit").hexdigest()
_RUN_ID = "unit-test-run-chunk-id-0000001"


# ---------------------------------------------------------------------------
# Reference implementation
#
# Mirrors Chunk.chunk_id written independently to cross-validate that the
# production code implements the correct identity formula.
# ---------------------------------------------------------------------------


def _reference_chunk_id(doc_id: str, start_page: int, chunk_index: int) -> str:
    """Canonical formula: SHA-256(doc_id + NUL + start_page + NUL + chunk_index)."""
    raw = f"{doc_id}\x00{start_page}\x00{chunk_index}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(
    *,
    doc_id: str = _DOC_ID,
    run_id: str = _RUN_ID,
    start_page: int,
    end_page: int | None = None,
    chunk_index: int,
    text: str = "Sample deterministic chunk text for unit tests.",
    quality_flags: tuple[ChunkQualityFlag, ...] = (),
) -> Chunk:
    """Construct a Chunk with the minimal fields required for chunk_id tests."""
    return Chunk(
        doc_id=doc_id,
        run_id=run_id,
        text=text,
        start_page=start_page,
        end_page=end_page if end_page is not None else start_page,
        chunk_index=chunk_index,
        quality_flags=quality_flags,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_chunk_id_deterministic() -> None:
    """Same doc_id + start_page + chunk_index → same chunk_id on every call.

    Validates both that chunk_id is stable (idempotent construction) and that
    it is a properly formatted 64-char lowercase SHA-256 hex digest.
    """
    chunk_a = _make_chunk(start_page=0, chunk_index=0)
    chunk_b = _make_chunk(start_page=0, chunk_index=0)

    assert chunk_a.chunk_id == chunk_b.chunk_id
    assert len(chunk_a.chunk_id) == 64
    assert chunk_a.chunk_id == chunk_a.chunk_id.lower()
    assert all(c in "0123456789abcdef" for c in chunk_a.chunk_id)


def test_chunk_id_matches_reference_implementation() -> None:
    """chunk_id matches the independently written reference formula.

    Cross-validates that Chunk.chunk_id encodes the identity contract from
    CLAUDE.md §5:
        SHA-256(doc_id + NUL + start_page + NUL + chunk_index)
    """
    chunk = _make_chunk(start_page=3, chunk_index=7)
    expected = _reference_chunk_id(chunk.doc_id, chunk.start_page, chunk.chunk_index)

    assert chunk.chunk_id == expected


def test_chunk_id_different_page() -> None:
    """Different start_page → different chunk_id, holding doc_id and chunk_index constant.

    Ensures the start_page component participates in chunk_id derivation so
    that a chunk beginning on page 2 is stored separately from one on page 5.
    """
    chunk_p2 = _make_chunk(start_page=2, chunk_index=0)
    chunk_p5 = _make_chunk(start_page=5, chunk_index=0)

    assert chunk_p2.chunk_id != chunk_p5.chunk_id


def test_chunk_id_different_index() -> None:
    """Different chunk_index → different chunk_id, holding doc_id and start_page constant.

    Ensures the chunk_index component participates in chunk_id derivation so
    that two adjacent chunks on the same page are individually addressable.
    """
    chunk_i0 = _make_chunk(start_page=1, chunk_index=0)
    chunk_i1 = _make_chunk(start_page=1, chunk_index=1)

    assert chunk_i0.chunk_id != chunk_i1.chunk_id


def test_chunk_id_idempotent() -> None:
    """Reprocessing the same document produces the same chunk_ids.

    Simulates two independent ingestion passes over the same document content
    (same doc_id, same page positions, same chunk indices).  Both passes must
    produce identical chunk_id sequences, guaranteeing that re-ingestion does
    not create duplicate vector entries in Qdrant (SPEC-03 S-03.5).
    """
    # First ingestion pass — chunks span three pages (two chunks per page)
    first_pass = [
        _make_chunk(start_page=i // 2, chunk_index=i)
        for i in range(6)
    ]
    first_ids = tuple(c.chunk_id for c in first_pass)

    # Second ingestion pass — identical inputs, independent Python objects
    second_pass = [
        _make_chunk(start_page=i // 2, chunk_index=i)
        for i in range(6)
    ]
    second_ids = tuple(c.chunk_id for c in second_pass)

    assert first_ids == second_ids


def test_chunk_ordering() -> None:
    """chunk_index is encoded in chunk_id: the ordered chunk_id sequence is stable
    and every position maps to exactly one unique chunk_id.

    Constructs N chunks in ascending chunk_index order, collects their IDs,
    then re-creates the same chunks and compares sequences.  Also asserts that
    no two chunk_indices share a chunk_id (collision resistance).

    This validates the DocumentManifest.chunk_ids ordering contract: consumers
    can reconstruct positional references from the tuple without re-parsing.
    """
    n_chunks = 5
    chunks = [_make_chunk(start_page=0, chunk_index=i) for i in range(n_chunks)]
    chunk_ids = [c.chunk_id for c in chunks]

    # All IDs are distinct — each chunk_index produces a unique chunk_id
    assert len(set(chunk_ids)) == n_chunks, (
        "Each chunk_index must produce a distinct chunk_id"
    )

    # Stable ordering: re-creating the same chunks yields the identical ID list
    same_chunks = [_make_chunk(start_page=0, chunk_index=i) for i in range(n_chunks)]
    same_ids = [c.chunk_id for c in same_chunks]

    assert chunk_ids == same_ids, (
        "chunk_id sequence must be identical across independent constructions"
    )


def test_chunk_id_null_byte_separator_prevents_boundary_collision() -> None:
    """Null-byte separator prevents boundary-collision between split inputs.

    Without the separator the byte sequences for two different (start_page,
    chunk_index) pairs could collide:
        start_page=1, chunk_index=23  →  "1" + "23"  =  b"123"
        start_page=12, chunk_index=3  →  "12" + "3"  =  b"123"

    The null-byte separator makes these inputs distinct so they produce
    different digests (Chunk.chunk_id docstring).
    """
    # These two parameter pairs have identical naive concatenations ("123")
    chunk_1_23 = _make_chunk(start_page=1, chunk_index=23)
    chunk_12_3 = _make_chunk(start_page=12, chunk_index=3)

    assert chunk_1_23.chunk_id != chunk_12_3.chunk_id, (
        "Boundary collision detected: null-byte separator is not working in chunk_id"
    )
