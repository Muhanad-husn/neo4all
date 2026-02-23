"""
tests/unit/test_manifest.py — DocumentManifest reuse, cache integration, idempotency
(SPEC-03 file 15).

Tests IngestorService manifest-based incremental rerun logic and ArtifactsService
cache integration.  No network, no S3, no Redis — all external I/O is mocked.

Acceptance criteria (SPEC-03 S-03.4 / SKILL-D R-D8):
  - Same content_hash + parser_config_hash → parse skipped; existing_manifest returned.
  - Different content_hash → stale manifest; re-parse logged with reason='content_changed'.
  - Different parser_config_hash → stale manifest; re-parse with reason='parser_config_changed'.
  - store_manifest() writes to S3 then calls cache.set with TTL=3600.
  - retrieve_manifest() cache hit → S3 not queried.
  - retrieve_manifest() cache miss → S3 queried; Redis backfilled after S3 hit.
  - Same file bytes + source_identity → same doc_id and chunk_ids (idempotency).

All tests are unit-level: no real S3, no real Redis, no real parsers.
asyncio_mode = "auto" (pyproject.toml) — no @pytest.mark.asyncio decorators needed.
"""

from __future__ import annotations

import hashlib
import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from api.cache.keys import CacheKey
from api.models.document import (
    Document,
    DocumentManifest,
    derive_content_hash,
    derive_parser_config_hash,
)
from api.services.ingestion import IngestorService
from api.storage.artifacts import ArtifactsService

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-manifest-00001"
_SOURCE_IDENTITY = "sample_document.pdf"

# Two distinct byte sequences — switching between them changes content_hash.
_FILE_BYTES = b"Test document content for manifest reuse tests."
_DIFFERENT_BYTES = b"Completely different document content - new file version."

# Pre-computed hashes for _FILE_BYTES (deterministic across all runs).
_CONTENT_HASH_A: str = derive_content_hash(_FILE_BYTES)
_CONTENT_HASH_B: str = derive_content_hash(_DIFFERENT_BYTES)

# doc_id derived from _FILE_BYTES — mirrors Document.doc_id computed field.
_DOC_ID_A: str = hashlib.sha256(
    f"{_RUN_ID}\x00{_SOURCE_IDENTITY}\x00{_CONTENT_HASH_A}".encode("utf-8")
).hexdigest()

# A stable chunk_id used in fixture manifests.
_CHUNK_ID_FIXTURE: str = hashlib.sha256(b"manifest-test-chunk-01").hexdigest()

# Successful parse text returned by the mocked Docling parser.
_PARSED_TEXT = "Parsed document text — Docling success."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_settings(
    *,
    docling: bool = True,
    unstructured: bool = True,
    raw: bool = True,
) -> types.SimpleNamespace:
    """Settings stub for IngestorService — parser toggle fields only.

    Avoids calling get_settings(), which requires all env vars to be present.
    """
    return types.SimpleNamespace(
        ENABLE_DOCLING=docling,
        ENABLE_UNSTRUCTURED=unstructured,
        ENABLE_RAW_FALLBACK=raw,
    )


def _storage_settings() -> types.SimpleNamespace:
    """Settings stub for ArtifactsService — S3 fields only."""
    return types.SimpleNamespace(
        S3_BUCKET_NAME="test-bucket",
        S3_ENDPOINT_URL="http://localhost:9000",
        S3_ACCESS_KEY_ID="test-key",
        S3_SECRET_ACCESS_KEY="test-secret",
    )


def _parser_config_hash_for(settings: types.SimpleNamespace) -> str:
    """Compute parser_config_hash matching what IngestorService._build_parser_config() produces.

    Keeps tests in sync with the service's own config serialisation logic.
    """
    config = {
        "enable_docling": settings.ENABLE_DOCLING,
        "enable_unstructured": settings.ENABLE_UNSTRUCTURED,
        "enable_raw_fallback": settings.ENABLE_RAW_FALLBACK,
    }
    return derive_parser_config_hash(config)


def _make_manifest(
    *,
    run_id: str = _RUN_ID,
    doc_id: str = _DOC_ID_A,
    content_hash: str = _CONTENT_HASH_A,
    parser_config_hash: str | None = None,
    chunk_ids: tuple[str, ...] = (_CHUNK_ID_FIXTURE,),
    timestamp: datetime | None = None,
) -> DocumentManifest:
    """Construct a DocumentManifest for use as a test fixture.

    parser_config_hash defaults to the hash derived from the all-enabled
    settings so tests can match it precisely without duplicating the formula.
    """
    if parser_config_hash is None:
        parser_config_hash = _parser_config_hash_for(_fake_settings())
    return DocumentManifest(
        run_id=run_id,
        doc_id=doc_id,
        content_hash=content_hash,
        parser_config_hash=parser_config_hash,
        chunk_ids=chunk_ids,
        timestamp=timestamp or datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
    )


def _make_ingestor(
    *,
    settings: types.SimpleNamespace | None = None,
    artifacts_service: object | None = None,
) -> IngestorService:
    """Construct IngestorService with injectable settings and ArtifactsService stub."""
    return IngestorService(
        settings=settings or _fake_settings(),
        artifacts_service=artifacts_service,
    )


def _mock_s3_for_get(raw_bytes: bytes) -> MagicMock:
    """S3 client mock: returns raw_bytes from get_object."""
    client = MagicMock()
    body = MagicMock()
    body.read.return_value = raw_bytes
    client.get_object.return_value = {"Body": body}
    return client


async def _immediate_to_thread(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    """Drop-in replacement for asyncio.to_thread that runs fn synchronously.

    Eliminates thread-pool overhead in unit tests while exercising the same
    synchronous helper functions that to_thread would execute in a worker thread.
    """
    return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# IngestorService — manifest reuse logic
# ---------------------------------------------------------------------------


async def test_manifest_reuse_same_content() -> None:
    """Same content_hash + parser_config_hash → parse skipped; existing_manifest returned.

    When retrieve_manifest() returns a manifest whose content_hash AND
    parser_config_hash both match the current document and configuration, the
    service must return immediately without invoking any parser tier.

    IngestResult on a valid cache hit:
      - existing_manifest: the matching DocumentManifest
      - extracted_text:    "" (no parsing)
      - document.parser_used: "cached" (sentinel for cache-hit path)
      - default_quality_flags: () (no parsing, no flags set)
    """
    settings = _fake_settings()
    parser_config_hash = _parser_config_hash_for(settings)

    valid_manifest = _make_manifest(
        content_hash=_CONTENT_HASH_A,
        parser_config_hash=parser_config_hash,
    )

    mock_artifacts = AsyncMock()
    mock_artifacts.retrieve_manifest.return_value = valid_manifest

    mock_parser = MagicMock()
    service = _make_ingestor(settings=settings, artifacts_service=mock_artifacts)

    with (
        patch("api.services.ingestion._parse_with_docling", mock_parser),
        patch("api.services.ingestion._parse_with_unstructured", mock_parser),
        patch("api.services.ingestion._parse_with_raw_text", mock_parser),
    ):
        result = await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    # No parser tier must have been invoked.
    mock_parser.assert_not_called()

    # retrieve_manifest was called with the doc_id derived from _FILE_BYTES.
    mock_artifacts.retrieve_manifest.assert_awaited_once_with(_RUN_ID, _DOC_ID_A)

    # Result carries the cached manifest and no extracted text.
    assert result.existing_manifest == valid_manifest
    assert result.extracted_text == ""
    assert result.document.parser_used == "cached"
    assert result.default_quality_flags == ()


async def test_manifest_invalidated_content_change() -> None:
    """Different content_hash in stored manifest → re-parse with reason='content_changed'.

    When retrieve_manifest() returns a manifest whose content_hash does not match
    the current file bytes, the service logs 'manifest_cache_miss' at INFO with
    reason='content_changed' and falls through to the parser chain.
    """
    settings = _fake_settings()
    parser_config_hash = _parser_config_hash_for(settings)

    # Stale manifest stored with _CONTENT_HASH_B; current file produces _CONTENT_HASH_A.
    stale_manifest = _make_manifest(
        content_hash=_CONTENT_HASH_B,
        parser_config_hash=parser_config_hash,
    )

    mock_artifacts = AsyncMock()
    mock_artifacts.retrieve_manifest.return_value = stale_manifest

    service = _make_ingestor(settings=settings, artifacts_service=mock_artifacts)

    with (
        patch("api.services.ingestion._parse_with_docling", return_value=_PARSED_TEXT),
        patch("api.services.ingestion.logger") as mock_logger,
    ):
        result = await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    # Re-parse occurred — no existing_manifest on result.
    assert result.existing_manifest is None
    assert result.extracted_text == _PARSED_TEXT

    # 'manifest_cache_miss' INFO event with reason='content_changed'.
    info_event_names = [c.args[0] for c in mock_logger.info.call_args_list]
    assert "manifest_cache_miss" in info_event_names, (
        "Expected 'manifest_cache_miss' INFO event when stored content_hash differs"
    )
    miss_call = next(
        c for c in mock_logger.info.call_args_list
        if c.args[0] == "manifest_cache_miss"
    )
    assert miss_call.kwargs["reason"] == "content_changed", (
        "Expected reason='content_changed' when only content_hash mismatches"
    )


async def test_manifest_invalidated_parser_change() -> None:
    """Same content_hash, different parser_config_hash → re-parse with reason='parser_config_changed'.

    When content_hash matches but parser_config_hash differs (e.g. a parser library
    was upgraded), the service logs 'manifest_cache_miss' at INFO with
    reason='parser_config_changed' and falls through to the parser chain.
    """
    settings = _fake_settings()
    current_parser_config_hash = _parser_config_hash_for(settings)

    # Different parser config — simulates a previous run with Docling disabled.
    old_parser_config_hash = derive_parser_config_hash(
        {"enable_docling": False, "enable_unstructured": True, "enable_raw_fallback": True}
    )
    assert old_parser_config_hash != current_parser_config_hash, (
        "Test precondition: old and current parser config hashes must differ"
    )

    stale_manifest = _make_manifest(
        content_hash=_CONTENT_HASH_A,           # content matches current file
        parser_config_hash=old_parser_config_hash,  # but parser config has changed
    )

    mock_artifacts = AsyncMock()
    mock_artifacts.retrieve_manifest.return_value = stale_manifest

    service = _make_ingestor(settings=settings, artifacts_service=mock_artifacts)

    with (
        patch("api.services.ingestion._parse_with_docling", return_value=_PARSED_TEXT),
        patch("api.services.ingestion.logger") as mock_logger,
    ):
        result = await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    # Re-parse occurred — no existing_manifest returned.
    assert result.existing_manifest is None
    assert result.extracted_text == _PARSED_TEXT

    # 'manifest_cache_miss' INFO event with reason='parser_config_changed'.
    info_event_names = [c.args[0] for c in mock_logger.info.call_args_list]
    assert "manifest_cache_miss" in info_event_names, (
        "Expected 'manifest_cache_miss' INFO event when parser_config_hash mismatches"
    )
    miss_call = next(
        c for c in mock_logger.info.call_args_list
        if c.args[0] == "manifest_cache_miss"
    )
    assert miss_call.kwargs["reason"] == "parser_config_changed", (
        "Expected reason='parser_config_changed' when content_hash matches but parser config differs"
    )


# ---------------------------------------------------------------------------
# ArtifactsService — cache integration
# ---------------------------------------------------------------------------


async def test_manifest_cache_integration() -> None:
    """store_manifest() writes to S3 then calls cache.set with key and TTL=3600.

    Verifies the write-through cache pattern (SKILL-D R-D8):
      1. S3 put_object is called with the serialised manifest JSON.
      2. cache.set is called with CacheKey.manifest(run_id, doc_id) and ttl=3600.

    A failed cache.set must not block the S3 write (tested via mock returning True).
    """
    manifest = _make_manifest()
    mock_s3 = MagicMock()
    mock_s3.put_object.return_value = {}

    mock_cache = AsyncMock()
    mock_cache.set.return_value = True

    service = ArtifactsService(settings=_storage_settings(), s3_client=mock_s3)

    with (
        patch("api.storage.artifacts.asyncio.to_thread", side_effect=_immediate_to_thread),
        patch("api.storage.artifacts.get_cache_client", return_value=mock_cache),
    ):
        s3_key = await service.store_manifest(_RUN_ID, _DOC_ID_A, manifest)

    # S3 put was called once with the manifest payload.
    mock_s3.put_object.assert_called_once()
    put_kwargs = mock_s3.put_object.call_args.kwargs
    assert put_kwargs["Bucket"] == "test-bucket"
    assert put_kwargs["Key"] == f"{_RUN_ID}/manifests/{_DOC_ID_A}.json"
    assert put_kwargs["ContentType"] == "application/json"

    # cache.set called with deterministic key and TTL=3600.
    expected_cache_key = CacheKey.manifest(run_id=_RUN_ID, doc_id=_DOC_ID_A)
    mock_cache.set.assert_awaited_once_with(expected_cache_key, manifest, ttl=3600)

    # Return value is the S3 key (for audit trail).
    assert s3_key == f"{_RUN_ID}/manifests/{_DOC_ID_A}.json"


async def test_manifest_cache_hit() -> None:
    """retrieve_manifest() returns cached manifest; S3 get_object is never called.

    On a Redis cache hit (cache.get returns a DocumentManifest), ArtifactsService
    must return the cached value immediately without performing any S3 I/O
    (SKILL-D R-D8 — cache-first reads).
    """
    manifest = _make_manifest()
    mock_s3 = MagicMock()

    mock_cache = AsyncMock()
    mock_cache.get.return_value = manifest  # Cache hit

    service = ArtifactsService(settings=_storage_settings(), s3_client=mock_s3)

    with patch("api.storage.artifacts.get_cache_client", return_value=mock_cache):
        result = await service.retrieve_manifest(_RUN_ID, _DOC_ID_A)

    # Correct manifest returned from cache.
    assert result == manifest

    # cache.get called with the deterministic CacheKey.manifest key.
    expected_key = CacheKey.manifest(run_id=_RUN_ID, doc_id=_DOC_ID_A)
    mock_cache.get.assert_awaited_once_with(expected_key, model=DocumentManifest)

    # S3 must not have been queried on a cache hit.
    mock_s3.get_object.assert_not_called()


async def test_manifest_cache_miss() -> None:
    """retrieve_manifest() cache miss → S3 queried; Redis backfilled on S3 hit.

    On a cache miss (cache.get returns None), ArtifactsService must:
      1. Fall through to S3 and deserialise the manifest JSON.
      2. Backfill Redis via cache.set so the next call is served from cache.

    The backfill uses the same TTL=3600 as the initial write-through
    (SKILL-D R-D8 — cache miss is not an error, R-D9).
    """
    manifest = _make_manifest()
    raw_bytes = manifest.model_dump_json().encode("utf-8")

    mock_s3 = _mock_s3_for_get(raw_bytes)

    mock_cache = AsyncMock()
    mock_cache.get.return_value = None   # Cache miss
    mock_cache.set.return_value = True

    service = ArtifactsService(settings=_storage_settings(), s3_client=mock_s3)

    with (
        patch("api.storage.artifacts.asyncio.to_thread", side_effect=_immediate_to_thread),
        patch("api.storage.artifacts.get_cache_client", return_value=mock_cache),
    ):
        result = await service.retrieve_manifest(_RUN_ID, _DOC_ID_A)

    # S3 was queried because cache missed.
    mock_s3.get_object.assert_called_once()

    # Correct manifest returned, deserialised from S3 bytes.
    assert result is not None
    assert result.doc_id == manifest.doc_id
    assert result.run_id == manifest.run_id
    assert result.content_hash == manifest.content_hash
    assert result.parser_config_hash == manifest.parser_config_hash
    assert result.chunk_ids == manifest.chunk_ids

    # Redis backfilled after S3 hit (TTL=3600).
    expected_key = CacheKey.manifest(run_id=_RUN_ID, doc_id=_DOC_ID_A)
    mock_cache.set.assert_awaited_once_with(expected_key, result, ttl=3600)


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_manifest_idempotent() -> None:
    """Same file bytes + source_identity → same doc_id and chunk_ids across reruns.

    Verifies that the deterministic ID derivation required by CLAUDE.md §4.3 / §5
    produces identical results across two independent ingestion calls:

      First call  — no cached manifest; parser runs; doc_id and content_hash recorded.
      Second call — matching manifest returned from cache; parser skipped; same doc_id.

    Also verifies that the reused manifest carries the same chunk_ids that were
    recorded after the first ingestion.  No uuid4() IDs are involved anywhere in
    this pipeline (CLAUDE.md §5 / §15 "Never Do").
    """
    settings = _fake_settings()
    parser_config_hash = _parser_config_hash_for(settings)

    # --- First ingestion: no existing manifest → parser runs ---
    mock_artifacts_first = AsyncMock()
    mock_artifacts_first.retrieve_manifest.return_value = None  # No manifest yet

    service_first = _make_ingestor(settings=settings, artifacts_service=mock_artifacts_first)

    with patch("api.services.ingestion._parse_with_docling", return_value=_PARSED_TEXT):
        result_first = await service_first.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    doc_id_first = result_first.document.doc_id
    content_hash_first = result_first.document.content_hash

    # Simulate the router creating and storing the manifest after chunking.
    stored_manifest = _make_manifest(
        run_id=_RUN_ID,
        doc_id=doc_id_first,
        content_hash=content_hash_first,
        parser_config_hash=parser_config_hash,
        chunk_ids=(_CHUNK_ID_FIXTURE,),
    )

    # --- Second ingestion: manifest available → parse skipped ---
    mock_artifacts_second = AsyncMock()
    mock_artifacts_second.retrieve_manifest.return_value = stored_manifest

    service_second = _make_ingestor(settings=settings, artifacts_service=mock_artifacts_second)

    mock_parser = MagicMock()
    with (
        patch("api.services.ingestion._parse_with_docling", mock_parser),
        patch("api.services.ingestion._parse_with_unstructured", mock_parser),
        patch("api.services.ingestion._parse_with_raw_text", mock_parser),
    ):
        result_second = await service_second.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    # No parser called on the second ingestion.
    mock_parser.assert_not_called()

    # doc_id is deterministic: same run_id + source_identity + content → same doc_id.
    doc_id_second = result_second.document.doc_id
    assert doc_id_first == doc_id_second, (
        "doc_id must be deterministic for identical inputs"
    )

    # content_hash is deterministic: same bytes → same SHA-256 digest.
    content_hash_second = result_second.document.content_hash
    assert content_hash_first == content_hash_second, (
        "content_hash must be deterministic for identical file bytes"
    )

    # Pre-computed doc_id must match what the service derived from _FILE_BYTES.
    assert doc_id_first == _DOC_ID_A, (
        "doc_id must match the pre-computed test constant derived from the same inputs"
    )

    # Reused manifest carries the same chunk_ids recorded after the first ingestion.
    assert result_second.existing_manifest is not None
    assert result_second.existing_manifest.chunk_ids == stored_manifest.chunk_ids
