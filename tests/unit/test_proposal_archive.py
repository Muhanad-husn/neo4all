"""
tests/unit/test_proposal_archive.py — Proposal archive tests.

No network, no LLM, no Neo4j.  S3 and Redis are replaced by in-memory stubs.

Coverage:
  - archive_for_run() copies proposals to archive prefix, deletes originals,
    writes manifest, and clears Redis index.
  - archive_for_run() with no proposals returns 0.
  - POST /agents/run returns 409 with pending_archive when proposals exist
    and confirm_archive is False.
  - POST /agents/run archives and proceeds when confirm_archive is True.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from api.proposals.models import ProposalClass, ProposalPacket
from api.proposals.service import ProposalService
from api.proposals.storage import ArchiveManifest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-archive-0001"
_CANDIDATE_ID = "c" * 64
_SCHEMA_VERSION = "sv_archive_fixture_v1"

_FAKE_SETTINGS = SimpleNamespace(
    S3_BUCKET_NAME="test-bucket",
    S3_ENDPOINT_URL="http://localhost:9000",
    S3_ACCESS_KEY_ID="test-key",
    S3_SECRET_ACCESS_KEY="test-secret",
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _FakeS3:
    """In-memory S3 stub — supports put_object, get_object, delete_object."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put_object(
        self, Bucket: str, Key: str, Body: bytes, ContentType: str = ""
    ) -> dict:
        self._store[Key] = Body
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict:
        if Key not in self._store:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self._store[Key])}

    def delete_object(self, Bucket: str, Key: str) -> dict:
        self._store.pop(Key, None)
        return {}


class _FakeCache:
    """In-memory cache stub implementing the CacheClient interface."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def get(self, key: str, model: Any = None) -> Any:
        raw = self._store.get(key)
        if raw is None:
            return None
        if model is not None and isinstance(raw, dict):
            return model.model_validate(raw)
        return raw

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        if hasattr(value, "model_dump"):
            self._store[key] = value.model_dump(mode="json")
        else:
            self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def scan_keys(self, prefix: str) -> list[str]:
        return [k for k in self._store if k.startswith(prefix)]


def _make_packet(**kwargs: Any) -> ProposalPacket:
    defaults: dict[str, Any] = dict(
        run_id=_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        proposal_class=ProposalClass.canonicalize,
        schema_version=_SCHEMA_VERSION,
        rationale="test archive rationale",
    )
    defaults.update(kwargs)
    return ProposalPacket(**defaults)


def _make_service(
    fake_cache: _FakeCache | None = None,
) -> tuple[ProposalService, _FakeS3, _FakeCache]:
    s3 = _FakeS3()
    cache = fake_cache or _FakeCache()
    with patch("api.proposals.service.get_cache_client", return_value=cache):
        svc = ProposalService(settings=_FAKE_SETTINGS, s3_client=s3)
    return svc, s3, cache


# ---------------------------------------------------------------------------
# archive_for_run tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_for_run_empty() -> None:
    """archive_for_run returns 0 when no proposals exist."""
    svc, _s3, _cache = _make_service()
    result = await svc.archive_for_run(_RUN_ID)
    assert result == 0


@pytest.mark.asyncio
async def test_archive_for_run_copies_and_deletes() -> None:
    """archive_for_run copies proposals to archive prefix, deletes originals,
    writes manifest, and clears the Redis index."""
    svc, s3, cache = _make_service()

    # Create a proposal first.
    packet = _make_packet()
    await svc.create(packet)

    # Verify it's in S3.
    original_key = f"{_RUN_ID}/proposals/{packet.proposal_id}.json"
    assert original_key in s3._store

    # Archive.
    archived = await svc.archive_for_run(_RUN_ID)
    assert archived == 1

    # Original should be deleted from S3.
    assert original_key not in s3._store

    # Should have archive copy + manifest in S3.
    archive_keys = [k for k in s3._store if "proposals_archive" in k]
    assert len(archive_keys) == 2  # one proposal + manifest

    # Verify manifest content.
    manifest_keys = [k for k in archive_keys if "manifest.json" in k]
    assert len(manifest_keys) == 1
    manifest_raw = s3._store[manifest_keys[0]]
    manifest = ArchiveManifest.model_validate_json(manifest_raw)
    assert manifest.run_id == _RUN_ID
    assert manifest.proposal_count == 1
    assert packet.proposal_id in manifest.proposal_ids

    # Redis index should be cleared.
    from api.cache.keys import CacheKey

    index = await cache.get(CacheKey.proposals_index(_RUN_ID))
    assert index is None

    # Individual proposal cache should be cleared.
    cached_proposal = await cache.get(CacheKey.proposal(packet.proposal_id))
    assert cached_proposal is None


@pytest.mark.asyncio
async def test_archive_for_run_multiple_proposals() -> None:
    """archive_for_run handles multiple proposals correctly."""
    svc, s3, cache = _make_service()

    # Create two proposals with different candidate IDs.
    p1 = _make_packet(candidate_id="a" * 64)
    p2 = _make_packet(candidate_id="b" * 64)
    await svc.create(p1)
    await svc.create(p2)

    archived = await svc.archive_for_run(_RUN_ID)
    assert archived == 2

    # No original proposal keys should remain.
    proposal_keys = [
        k for k in s3._store if "/proposals/" in k and "archive" not in k
    ]
    assert len(proposal_keys) == 0

    # Manifest should list both.
    manifest_keys = [k for k in s3._store if "manifest.json" in k]
    manifest = ArchiveManifest.model_validate_json(s3._store[manifest_keys[0]])
    assert manifest.proposal_count == 2
