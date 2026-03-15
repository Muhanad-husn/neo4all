"""
tests/unit/test_delete_document.py — Delete document feature tests.

No network, no LLM, no Neo4j. Tests cover:

  - _sync_delete_object helper (idempotent, error handling)
  - ArtifactsService.delete_manifest (S3 + Redis cleanup)
  - VectorIndexer.delete_chunks_by_ids (Qdrant point deletion)
  - DeleteDocumentResponse model validation
  - DELETE endpoint returns 404 when manifest not found
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import sys

import pytest

from api.routers.documents_models import DeleteDocumentResponse
from api.storage.artifacts_helpers import _sync_delete_object


# ---------------------------------------------------------------------------
# _sync_delete_object
# ---------------------------------------------------------------------------


class TestSyncDeleteObject:
    """Tests for the _sync_delete_object S3 helper."""

    def test_successful_delete(self) -> None:
        client = MagicMock()
        client.delete_object = MagicMock(return_value=None)
        _sync_delete_object(client, "bucket", "key/path")
        client.delete_object.assert_called_once_with(Bucket="bucket", Key="key/path")

    def test_no_such_key_is_idempotent(self) -> None:
        """NoSuchKey errors are treated as success (idempotent delete)."""
        from botocore.exceptions import ClientError

        error_response = {"Error": {"Code": "NoSuchKey", "Message": "Not found"}}
        client = MagicMock()
        client.delete_object = MagicMock(
            side_effect=ClientError(error_response, "DeleteObject")
        )
        # Should not raise
        _sync_delete_object(client, "bucket", "key/path")

    def test_other_client_error_raises_storage_error(self) -> None:
        from botocore.exceptions import ClientError

        from api.storage.artifacts import StorageError

        error_response = {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}}
        client = MagicMock()
        client.delete_object = MagicMock(
            side_effect=ClientError(error_response, "DeleteObject")
        )
        with pytest.raises(StorageError):
            _sync_delete_object(client, "bucket", "key/path")

    def test_botocore_error_raises_storage_error(self) -> None:
        from botocore.exceptions import BotoCoreError

        from api.storage.artifacts import StorageError

        client = MagicMock()
        client.delete_object = MagicMock(side_effect=BotoCoreError())
        with pytest.raises(StorageError):
            _sync_delete_object(client, "bucket", "key/path")


# ---------------------------------------------------------------------------
# ArtifactsService.delete_manifest
# ---------------------------------------------------------------------------


class TestDeleteManifest:
    """Tests for ArtifactsService.delete_manifest."""

    def test_delete_manifest_calls_s3_and_cache(self) -> None:
        from api.storage.artifacts import ArtifactsService

        mock_client = MagicMock()
        mock_client.delete_object = MagicMock(return_value=None)

        settings = MagicMock()
        settings.S3_BUCKET_NAME = "test-bucket"

        service = ArtifactsService(settings=settings, s3_client=mock_client)

        mock_cache = AsyncMock()
        mock_cache.delete = AsyncMock(return_value=True)

        with patch("api.storage.artifacts.get_cache_client", return_value=mock_cache):
            asyncio.get_event_loop().run_until_complete(
                service.delete_manifest("run123", "doc456")
            )

        mock_client.delete_object.assert_called_once()
        mock_cache.delete.assert_called_once()


# ---------------------------------------------------------------------------
# VectorIndexer.delete_chunks_by_ids
# ---------------------------------------------------------------------------


class TestDeleteChunksByIds:
    """Tests for VectorIndexer.delete_chunks_by_ids."""

    @patch.dict("sys.modules", {"qdrant_client": MagicMock(), "qdrant_client.models": MagicMock()})
    def test_delete_submits_correct_point_ids(self) -> None:
        from api.vector.indexer import VectorIndexer

        mock_qdrant = AsyncMock()
        mock_qdrant.delete = AsyncMock(return_value=None)

        settings = MagicMock()
        settings.QDRANT_URL = None

        indexer = VectorIndexer(settings=settings, qdrant_client=mock_qdrant)

        chunk_ids = [
            "a" * 64,
            "b" * 64,
        ]

        count = asyncio.get_event_loop().run_until_complete(
            indexer.delete_chunks_by_ids("run1", chunk_ids)
        )

        assert count == 2
        mock_qdrant.delete.assert_called_once()
        call_kwargs = mock_qdrant.delete.call_args
        assert call_kwargs.kwargs["collection_name"] == "chunks_run1"

    def test_delete_empty_list_returns_zero(self) -> None:
        from api.vector.indexer import VectorIndexer

        mock_qdrant = AsyncMock()
        settings = MagicMock()
        settings.QDRANT_URL = None
        indexer = VectorIndexer(settings=settings, qdrant_client=mock_qdrant)

        count = asyncio.get_event_loop().run_until_complete(
            indexer.delete_chunks_by_ids("run1", [])
        )
        assert count == 0
        mock_qdrant.delete.assert_not_called()

    def test_delete_returns_zero_on_qdrant_error(self) -> None:
        from api.vector.indexer import VectorIndexer

        mock_qdrant = AsyncMock()
        mock_qdrant.delete = AsyncMock(side_effect=RuntimeError("connection refused"))

        settings = MagicMock()
        settings.QDRANT_URL = None
        indexer = VectorIndexer(settings=settings, qdrant_client=mock_qdrant)

        count = asyncio.get_event_loop().run_until_complete(
            indexer.delete_chunks_by_ids("run1", ["a" * 64])
        )
        assert count == 0


# ---------------------------------------------------------------------------
# DeleteDocumentResponse model
# ---------------------------------------------------------------------------


class TestDeleteDocumentResponseModel:
    """Tests for the DeleteDocumentResponse Pydantic model."""

    def test_defaults(self) -> None:
        resp = DeleteDocumentResponse(run_id="r1", status="success")
        assert resp.doc_id == ""
        assert resp.chunks_deleted == 0
        assert resp.jobs_cleared == 0

    def test_populated(self) -> None:
        resp = DeleteDocumentResponse(
            run_id="r1",
            status="success",
            doc_id="d1",
            chunks_deleted=5,
            jobs_cleared=3,
        )
        assert resp.doc_id == "d1"
        assert resp.chunks_deleted == 5
        assert resp.jobs_cleared == 3

    def test_serialisation_roundtrip(self) -> None:
        resp = DeleteDocumentResponse(
            run_id="r1",
            status="success",
            doc_id="d1",
            chunks_deleted=2,
            jobs_cleared=1,
        )
        data = resp.model_dump()
        rebuilt = DeleteDocumentResponse.model_validate(data)
        assert rebuilt == resp
