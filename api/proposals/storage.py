"""api/proposals/storage.py — S3 storage helpers for the proposal service.

Extracted from api/proposals/service.py to keep the ProposalService class
focused on lifecycle orchestration.  All S3 I/O details live here:

  _ProposalIndex       — Pydantic wrapper for the per-run proposal-id list.
  _proposal_s3_key()   — Build the S3 object key for a serialised proposal.
  _sync_put_proposal() — Synchronous PUT (run inside asyncio.to_thread).
  _sync_get_proposal() — Synchronous GET (run inside asyncio.to_thread).

ProposalStorageError is defined here because the sync helpers raise it
directly.  service.py re-exports the class so all existing import paths
(e.g. ``from api.proposals.service import ProposalStorageError``) remain
valid.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Storage exception
# ---------------------------------------------------------------------------


class ProposalStorageError(Exception):
    """Raised for unrecoverable S3 write or read failures.

    Attributes:
        code:    Machine-readable error code (e.g. "s3_put_failed").
        message: Human-readable description.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Redis index model — wraps a list of proposal_ids for typed cache storage
# ---------------------------------------------------------------------------


class _ProposalIndex(BaseModel):
    """Thin Pydantic wrapper for a per-run ordered list of proposal_ids.

    Required because CacheClient.get/set only accept Pydantic BaseModel
    instances (SKILL-A R-A3 — no raw dicts at module boundaries).
    """

    proposal_ids: list[str] = []


# ---------------------------------------------------------------------------
# S3 key builders — single place for path layout (SKILL-C R-C6)
# ---------------------------------------------------------------------------


def _proposal_s3_key(run_id: str, proposal_id: str) -> str:
    """S3 key for a serialised ProposalPacket."""
    return f"{run_id}/proposals/{proposal_id}.json"


def _archive_s3_key(run_id: str, timestamp: str, proposal_id: str) -> str:
    """S3 key for an archived proposal inside a timestamped prefix."""
    return f"{run_id}/proposals_archive/{timestamp}/{proposal_id}.json"


def _archive_manifest_key(run_id: str, timestamp: str) -> str:
    """S3 key for the archive manifest."""
    return f"{run_id}/proposals_archive/{timestamp}/manifest.json"


class ArchiveManifest(BaseModel):
    """Metadata written alongside archived proposals."""

    run_id: str
    archived_at: str
    proposal_count: int
    proposal_ids: list[str]


# ---------------------------------------------------------------------------
# Synchronous S3 helpers — always run inside asyncio.to_thread
# ---------------------------------------------------------------------------


def _sync_put_proposal(
    client: Any,
    bucket: str,
    key: str,
    body: bytes,
) -> None:
    """PUT proposal JSON to S3; raise ProposalStorageError on failure."""
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
    except (BotoCoreError, ClientError) as exc:
        raise ProposalStorageError(
            code="s3_put_failed",
            message=f"S3 put failed for key {key!r}: {exc}",
        ) from exc


def _sync_get_proposal(
    client: Any,
    bucket: str,
    key: str,
) -> bytes | None:
    """GET proposal JSON from S3.

    Returns raw bytes on hit, None on 404 / NoSuchKey.
    Raises ProposalStorageError for all other failures.
    """
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404"):
            return None
        raise ProposalStorageError(
            code="s3_get_failed",
            message=f"S3 get failed for key {key!r}: {exc}",
        ) from exc
    except BotoCoreError as exc:
        raise ProposalStorageError(
            code="s3_get_failed",
            message=f"S3 get failed for key {key!r}: {exc}",
        ) from exc
