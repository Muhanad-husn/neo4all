"""
api/storage/artifacts.py — Artifact storage via boto3 (SPEC-03 S-03.4).

Provides three operations over RustFS (local) / S3 (production):

  store_raw_document(run_id, doc_id, file_bytes) → S3 key
  store_manifest(run_id, doc_id, manifest)       → S3 key  (write-through to Redis)
  retrieve_manifest(run_id, doc_id)              → DocumentManifest | None

S3 path layout
--------------
  {run_id}/documents/{doc_id}/raw     — raw document bytes (application/octet-stream)
  {run_id}/manifests/{doc_id}.json    — serialised DocumentManifest (application/json)

Caching (SKILL-D R-D8)
-----------------------
  store_manifest()    writes to S3 then caches in Redis (TTL = 3600 s).
  retrieve_manifest() checks Redis first; falls back to S3 on miss;
                      backfills Redis on S3 hit.
  Redis failures degrade gracefully — fall through to S3, never raise
  (SKILL-D R-D9).

All boto3 calls are wrapped with asyncio.to_thread (boto3 is synchronous).

StorageError
------------
Raised for unrecoverable S3 failures. Callers catch this and emit a
structured API error response. Cache failures never raise StorageError.
"""

from __future__ import annotations

import asyncio
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.models.document import DocumentManifest
from api.observability.logger import get_logger

logger = get_logger(__name__)

# Cache TTL for manifests per SKILL-D R-D8 table (1 hour)
_MANIFEST_CACHE_TTL: int = 3600


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class StorageError(Exception):
    """Raised for unrecoverable S3/RustFS operation failures.

    Attributes:
        code:    Machine-readable error code (e.g. "s3_put_failed").
        message: Human-readable description of the failure.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# S3 key builders — deterministic, single place to change path layout
# ---------------------------------------------------------------------------

def _raw_document_key(run_id: str, doc_id: str) -> str:
    return f"{run_id}/documents/{doc_id}/raw"


def _manifest_key(run_id: str, doc_id: str) -> str:
    return f"{run_id}/manifests/{doc_id}.json"


# ---------------------------------------------------------------------------
# Synchronous S3 helpers — always run inside asyncio.to_thread
# ---------------------------------------------------------------------------

def _sync_put_object(
    client: Any,
    bucket: str,
    key: str,
    body: bytes,
    content_type: str,
) -> None:
    """PUT body to S3; raise StorageError on any boto3 failure."""
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )
    except (BotoCoreError, ClientError) as exc:
        raise StorageError(
            code="s3_put_failed",
            message=f"S3 put failed for key {key!r}: {exc}",
        ) from exc


def _sync_get_object(client: Any, bucket: str, key: str) -> bytes | None:
    """GET object from S3.

    Returns raw bytes on success, None if the key does not exist (404 /
    NoSuchKey). Raises StorageError for all other S3 / network failures.
    """
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404"):
            return None
        raise StorageError(
            code="s3_get_failed",
            message=f"S3 get failed for key {key!r}: {exc}",
        ) from exc
    except BotoCoreError as exc:
        raise StorageError(
            code="s3_get_failed",
            message=f"S3 get failed for key {key!r}: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# ArtifactsService
# ---------------------------------------------------------------------------

class ArtifactsService:
    """boto3-backed storage for raw documents and manifests.

    Encapsulates all S3/RustFS interactions. boto3 S3 clients are thread-safe
    for concurrent get/put; a single client is shared for the service lifetime.

    Manifests use a write-through / read-through Redis cache pattern:
      - store_manifest()    writes to S3, then caches in Redis (TTL = 3600 s).
      - retrieve_manifest() checks Redis; falls back to S3; backfills on S3 hit.
    Redis failures always degrade gracefully (SKILL-D R-D9).

    Args:
        settings:   Application settings. Defaults to the process singleton.
        s3_client:  Pre-built boto3 S3 client. Injectable for unit testing.
    """

    def __init__(
        self,
        settings: Any = None,
        s3_client: Any = None,
    ) -> None:
        from api.config import get_settings

        s = settings or get_settings()
        self._bucket: str = s.S3_BUCKET_NAME

        if s3_client is not None:
            self._client: Any = s3_client
        else:
            self._client = boto3.client(
                "s3",
                endpoint_url=s.S3_ENDPOINT_URL,
                aws_access_key_id=s.S3_ACCESS_KEY_ID,
                aws_secret_access_key=s.S3_SECRET_ACCESS_KEY,
            )

    # ------------------------------------------------------------------
    # store_raw_document
    # ------------------------------------------------------------------

    async def store_raw_document(
        self,
        run_id: str,
        doc_id: str,
        file_bytes: bytes,
    ) -> str:
        """Store raw document bytes in S3.

        Args:
            run_id:     Governed run identifier.
            doc_id:     64-char deterministic document identifier.
            file_bytes: Raw bytes of the source document. Never logged
                        (SKILL-D R-D5).

        Returns:
            S3 object key (for audit trail).

        Raises:
            StorageError: On S3 put failure.

        Log events:
            raw_document_stored  INFO  — run_id, doc_id, s3_key, size_bytes
            storage_error        ERROR — run_id, doc_id, s3_key, error, operation
        """
        key = _raw_document_key(run_id, doc_id)
        try:
            await asyncio.to_thread(
                _sync_put_object,
                self._client,
                self._bucket,
                key,
                file_bytes,
                "application/octet-stream",
            )
        except StorageError as exc:
            logger.error(
                "storage_error",
                run_id=run_id,
                doc_id=doc_id,
                s3_key=key,
                error=exc.message,
                operation="store_raw_document",
            )
            raise

        logger.info(
            "raw_document_stored",
            run_id=run_id,
            doc_id=doc_id,
            s3_key=key,
            size_bytes=len(file_bytes),
        )
        return key

    # ------------------------------------------------------------------
    # store_json — generic JSON artifact storage (SPEC-08 S-08.1)
    # ------------------------------------------------------------------

    async def store_json(self, s3_key: str, data: bytes) -> str:
        """Store a JSON artifact at an explicit S3 key.

        Used by the dry-run path (SPEC-08 S-08.1) to persist DiffPlan
        payloads without coupling to a fixed key layout.

        Args:
            s3_key: Full S3 object key (caller determines path layout).
            data:   UTF-8 encoded JSON bytes.

        Returns:
            The S3 key written (for logging / audit linkage).

        Raises:
            StorageError: On S3 put failure.
        """
        await asyncio.to_thread(
            _sync_put_object,
            self._client,
            self._bucket,
            s3_key,
            data,
            "application/json",
        )
        return s3_key

    # ------------------------------------------------------------------
    # store_manifest
    # ------------------------------------------------------------------

    async def store_manifest(
        self,
        run_id: str,
        doc_id: str,
        manifest: DocumentManifest,
    ) -> str:
        """Serialise and store a DocumentManifest in S3; write-through to Redis.

        Write-through: after a successful S3 put the manifest is placed in
        Redis (TTL = 3600 s) so subsequent retrieve_manifest() calls are
        served from memory. Cache write failures are logged at WARNING and do
        not block the return.

        Args:
            run_id:   Governed run identifier.
            doc_id:   64-char deterministic document identifier.
            manifest: Completed DocumentManifest (chunk_ids fully populated).

        Returns:
            S3 object key (for audit trail).

        Raises:
            StorageError: On S3 put failure. Cache write failures never raise.

        Log events:
            manifest_stored  INFO  — run_id, doc_id, s3_key, chunk_count
            manifest_cached  DEBUG — run_id, doc_id, cache_key, ttl
            storage_error    ERROR — run_id, doc_id, s3_key, error, operation
        """
        key = _manifest_key(run_id, doc_id)
        payload: bytes = manifest.model_dump_json().encode("utf-8")
        try:
            await asyncio.to_thread(
                _sync_put_object,
                self._client,
                self._bucket,
                key,
                payload,
                "application/json",
            )
        except StorageError as exc:
            logger.error(
                "storage_error",
                run_id=run_id,
                doc_id=doc_id,
                s3_key=key,
                error=exc.message,
                operation="store_manifest",
            )
            raise

        logger.info(
            "manifest_stored",
            run_id=run_id,
            doc_id=doc_id,
            s3_key=key,
            chunk_count=len(manifest.chunk_ids),
        )

        # Write-through: populate cache after successful S3 put.
        cache = get_cache_client()
        cache_key = CacheKey.manifest(run_id=run_id, doc_id=doc_id)
        if await cache.set(cache_key, manifest, ttl=_MANIFEST_CACHE_TTL):
            logger.debug(
                "manifest_cached",
                run_id=run_id,
                doc_id=doc_id,
                cache_key=cache_key,
                ttl=_MANIFEST_CACHE_TTL,
            )

        return key

    # ------------------------------------------------------------------
    # list_manifests_for_run
    # ------------------------------------------------------------------

    async def list_manifests_for_run(self, run_id: str) -> list[DocumentManifest]:
        """List all DocumentManifests stored in S3 for a run.

        Uses boto3 list_objects_v2 with the run's manifest prefix to discover
        all doc_ids, then fetches each manifest via retrieve_manifest() (which
        is cache-first).  Returns an empty list when no manifests exist for
        the run.

        Args:
            run_id: Governed run identifier.

        Returns:
            List of DocumentManifest objects sorted by doc_id (stable order).

        Raises:
            StorageError: On S3 list failure (not on empty result).

        Log events:
            manifests_listed  INFO  — run_id, count
            storage_error     ERROR — run_id, error, operation
        """
        prefix = f"{run_id}/manifests/"

        def _sync_list_keys() -> list[str]:
            paginator = self._client.get_paginator("list_objects_v2")
            keys: list[str] = []
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
            return keys

        try:
            keys = await asyncio.to_thread(_sync_list_keys)
        except (BotoCoreError, ClientError) as exc:
            logger.error(
                "storage_error",
                run_id=run_id,
                error=str(exc),
                operation="list_manifests_for_run",
            )
            raise StorageError(
                code="s3_list_failed",
                message=f"S3 list failed for run {run_id!r}: {exc}",
            ) from exc

        # Extract doc_ids from keys: "{run_id}/manifests/{doc_id}.json"
        doc_ids = sorted(
            k[len(prefix) : -len(".json")]
            for k in keys
            if k.endswith(".json")
        )

        manifests: list[DocumentManifest] = []
        for doc_id in doc_ids:
            manifest = await self.retrieve_manifest(run_id, doc_id)
            if manifest is not None:
                manifests.append(manifest)

        logger.info("manifests_listed", run_id=run_id, count=len(manifests))
        return manifests

    # ------------------------------------------------------------------
    # retrieve_manifest
    # ------------------------------------------------------------------

    async def retrieve_manifest(
        self,
        run_id: str,
        doc_id: str,
    ) -> DocumentManifest | None:
        """Retrieve a DocumentManifest; Redis cache first, then S3 fallback.

        Read-through cache:
          1. Check Redis via CacheKey.manifest(run_id, doc_id).
          2. On cache miss, check S3.
          3. On S3 hit, backfill Redis (amortises future round-trips).

        Redis failures degrade gracefully: fall through to S3, never raise
        (SKILL-D R-D9).

        Args:
            run_id: Governed run identifier.
            doc_id: 64-char deterministic document identifier.

        Returns:
            DocumentManifest on success, None if not found in cache or S3.

        Raises:
            StorageError: On S3 get failure (not on cache miss or cache error).

        Log events:
            manifest_retrieved  DEBUG — run_id, doc_id, source ("cache" | "s3")
            manifest_not_found  DEBUG — run_id, doc_id
            storage_error       ERROR — run_id, doc_id, s3_key, error, operation
        """
        cache_key = CacheKey.manifest(run_id=run_id, doc_id=doc_id)
        cache = get_cache_client()

        # --- Redis check (SKILL-D R-D8) ---
        cached = await cache.get(cache_key, model=DocumentManifest)
        if cached is not None:
            logger.debug(
                "manifest_retrieved",
                run_id=run_id,
                doc_id=doc_id,
                source="cache",
            )
            return cached

        # --- S3 fallback ---
        key = _manifest_key(run_id, doc_id)
        try:
            raw = await asyncio.to_thread(
                _sync_get_object, self._client, self._bucket, key
            )
        except StorageError as exc:
            logger.error(
                "storage_error",
                run_id=run_id,
                doc_id=doc_id,
                s3_key=key,
                error=exc.message,
                operation="retrieve_manifest",
            )
            raise

        if raw is None:
            logger.debug("manifest_not_found", run_id=run_id, doc_id=doc_id)
            return None

        manifest = DocumentManifest.model_validate_json(raw)
        logger.debug(
            "manifest_retrieved",
            run_id=run_id,
            doc_id=doc_id,
            source="s3",
        )

        # Backfill cache so the next retrieve_manifest() is served from Redis.
        await cache.set(cache_key, manifest, ttl=_MANIFEST_CACHE_TTL)

        return manifest


# ---------------------------------------------------------------------------
# Process-level singleton factory
# ---------------------------------------------------------------------------

_service_instance: ArtifactsService | None = None


def get_artifacts_service() -> ArtifactsService:
    """Return the process-level ArtifactsService singleton.

    Created on first call (lazy). Settings are resolved at that point so
    startup env-var validation has already run.
    """
    global _service_instance
    if _service_instance is None:
        _service_instance = ArtifactsService()
    return _service_instance
