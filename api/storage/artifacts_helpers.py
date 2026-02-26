"""S3 key builders and synchronous I/O wrappers for ArtifactsService.

These pure functions are extracted from api.storage.artifacts to keep
that module focused on the async ArtifactsService class.

S3 key builders
---------------
  _raw_document_key(run_id, doc_id) → "{run_id}/documents/{doc_id}/raw"
  _manifest_key(run_id, doc_id)     → "{run_id}/manifests/{doc_id}.json"

Sync I/O wrappers
-----------------
  _sync_put_object(client, bucket, key, body, content_type)  → None | raise StorageError
  _sync_get_object(client, bucket, key)                      → bytes | None | raise StorageError

All sync wrappers are designed to be called via ``asyncio.to_thread``
from async callers. They never perform async I/O themselves.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError


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
    # Import here to avoid a circular dependency at module level.
    from api.storage.artifacts import StorageError

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
    # Import here to avoid a circular dependency at module level.
    from api.storage.artifacts import StorageError

    try:
        response = client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "NoSuchBucket", "404"):
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
