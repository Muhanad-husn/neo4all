"""api/services/curation/snapshots.py — Pre-merge snapshot persistence to S3.

Captures a full snapshot of a node (properties + relationships) before a merge
operation, enabling reversibility.  Snapshots are stored at:

    {run_id}/snapshots/{dedupe_key}.json

Uses the existing boto3 pattern from api/storage/artifacts.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from api.observability.logger import get_logger
from api.storage.artifacts import ArtifactsService, get_artifacts_service

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Snapshot model
# ---------------------------------------------------------------------------


class MergeSnapshot(BaseModel):
    """Full snapshot of a node before merge, stored to S3 for reversibility."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    schema_version: str
    dedupe_key: str
    node_type: str
    properties: dict[str, Any]
    outgoing_rels: tuple[dict[str, Any], ...]
    incoming_rels: tuple[dict[str, Any], ...]
    snapshot_id: str
    created_at: str


# ---------------------------------------------------------------------------
# Snapshot ID derivation (deterministic)
# ---------------------------------------------------------------------------


def _derive_snapshot_id(run_id: str, dedupe_key: str, timestamp: str) -> str:
    """SHA-256 of (run_id, dedupe_key, timestamp) for deterministic identity."""
    payload = json.dumps(
        {"run_id": run_id, "dedupe_key": dedupe_key, "timestamp": timestamp},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def save_snapshot(snapshot: MergeSnapshot) -> str:
    """Persist snapshot to S3 at {run_id}/snapshots/{dedupe_key}.json.

    Returns the S3 key on success.
    """
    s3_key = f"{snapshot.run_id}/snapshots/{snapshot.dedupe_key}.json"
    artifacts = get_artifacts_service()
    data = snapshot.model_dump_json().encode("utf-8")
    await artifacts.store_json(s3_key, data)
    logger.info(
        "merge_snapshot_saved",
        run_id=snapshot.run_id,
        dedupe_key=snapshot.dedupe_key,
        s3_key=s3_key,
    )
    return s3_key


async def load_snapshot(run_id: str, dedupe_key: str) -> MergeSnapshot | None:
    """Retrieve snapshot for potential merge reversal.

    Returns None if the snapshot does not exist.
    """
    s3_key = f"{run_id}/snapshots/{dedupe_key}.json"
    artifacts = get_artifacts_service()

    def _sync_get() -> bytes | None:
        try:
            response = artifacts._client.get_object(
                Bucket=artifacts._bucket, Key=s3_key
            )
            return response["Body"].read()
        except Exception:
            return None

    raw = await asyncio.to_thread(_sync_get)
    if raw is None:
        return None
    return MergeSnapshot.model_validate_json(raw)


def build_snapshot(
    *,
    run_id: str,
    schema_version: str,
    dedupe_key: str,
    node_type: str,
    properties: dict[str, Any],
    outgoing_rels: list[dict[str, Any]],
    incoming_rels: list[dict[str, Any]],
) -> MergeSnapshot:
    """Construct a MergeSnapshot with deterministic snapshot_id."""
    now = datetime.now(tz=timezone.utc).isoformat()
    return MergeSnapshot(
        run_id=run_id,
        schema_version=schema_version,
        dedupe_key=dedupe_key,
        node_type=node_type,
        properties=properties,
        outgoing_rels=tuple(outgoing_rels),
        incoming_rels=tuple(incoming_rels),
        snapshot_id=_derive_snapshot_id(run_id, dedupe_key, now),
        created_at=now,
    )
