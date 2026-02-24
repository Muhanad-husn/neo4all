"""
tests/unit/test_worker_jobs.py — ARQ extraction job behavior (SPEC-04 file 14).

Validates:
  - Job arguments (run_id, chunk_id, doc_id) are JSON-serializable primitive strings.
  - ExtractionError raised by ExtractionService is caught; job returns None (no raise).
  - RuntimeError raised by GraphWriter is caught; job returns None (no raise).
  - A ChunkJobStatus with status="failed" is written to the Redis mock on either failure.
  - An already-complete job is skipped without re-running extraction or graph writes.

Mocking strategy:
  - api.worker.jobs.get_cache_client → AsyncMock cache that tracks set() calls
  - api.worker.jobs.get_extraction_service → mock ExtractionService instance
  - api.worker.jobs.get_vector_indexer → mock VectorIndexer (supplies chunk text)
  - api.worker.jobs.GraphWriter → mock class whose instances expose write_extraction_result
  - ctx dict carries a MagicMock neo4j_client (never called in these tests)

No network, no Neo4j, no LLM.
asyncio_mode = "auto" (pyproject.toml) — no @pytest.mark.asyncio decorators needed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from api.graph.models import ExtractionWriteResult
from api.models.extraction import ExtractionResult
from api.services.extraction import ExtractionError
from api.worker.jobs import ChunkJobStatus, extraction_job

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-worker-000001"
_CHUNK_ID = "e" * 64
_DOC_ID = "f" * 64
_SCHEMA_VERSION = "g" * 64
_CHUNK_TEXT = "Sample paragraph text for unit testing. No real LLM involved."


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _make_write_result(
    nodes_created: int = 1,
    nodes_matched: int = 0,
    edges_created: int = 0,
    edges_matched: int = 0,
    edges_skipped: int = 0,
) -> ExtractionWriteResult:
    return ExtractionWriteResult(
        run_id=_RUN_ID,
        chunk_id=_CHUNK_ID,
        schema_version=_SCHEMA_VERSION,
        nodes_created=nodes_created,
        nodes_matched=nodes_matched,
        edges_created=edges_created,
        edges_matched=edges_matched,
        edges_skipped=edges_skipped,
        node_results=(),
        edge_results=(),
    )


def _make_extraction_result() -> ExtractionResult:
    return ExtractionResult(
        run_id=_RUN_ID,
        chunk_id=_CHUNK_ID,
        schema_version=_SCHEMA_VERSION,
    )


def _make_ctx() -> dict:
    """ARQ context dict with a mock Neo4jClient."""
    return {"neo4j_client": MagicMock()}


# ---------------------------------------------------------------------------
# Shared patch context — eliminates boilerplate across test classes
# ---------------------------------------------------------------------------


class _PatchedJob:
    """Context manager that patches all external dependencies of extraction_job.

    Usage:
        with _PatchedJob() as p:
            p.cache.get.return_value = None          # job status miss
            p.indexer.get_chunk_text.return_value = "text"
            p.extraction_svc.extract_chunk.return_value = _make_extraction_result()
            p.writer_instance.write_extraction_result.return_value = _make_write_result()
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)
            p.cache.set.assert_called()
    """

    def __init__(self) -> None:
        self.cache = AsyncMock()
        self.indexer = AsyncMock()
        self.extraction_svc = AsyncMock()
        self.writer_instance = AsyncMock()

        # Default: all gets return None (no cached job status, no cached chunk text)
        self.cache.get.return_value = None

        # Default: Qdrant returns chunk text so the job doesn't fail at retrieval
        self.indexer.get_chunk_text.return_value = _CHUNK_TEXT

        # Patch targets are the names *as imported* in api.worker.jobs
        self._patches = [
            patch("api.worker.jobs.get_cache_client", return_value=self.cache),
            patch("api.worker.jobs.get_vector_indexer", return_value=self.indexer),
            patch(
                "api.worker.jobs.get_extraction_service",
                return_value=self.extraction_svc,
            ),
            patch(
                "api.worker.jobs.GraphWriter",
                return_value=self.writer_instance,
            ),
        ]

    def __enter__(self) -> "_PatchedJob":
        for p in self._patches:
            p.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        for p in reversed(self._patches):
            p.__exit__(*args)


# ---------------------------------------------------------------------------
# Helpers for asserting on set() calls
# ---------------------------------------------------------------------------


def _status_set_calls(mock_cache: AsyncMock) -> list[ChunkJobStatus]:
    """Return all ChunkJobStatus values passed to cache.set(), in call order."""
    statuses = []
    for c in mock_cache.set.call_args_list:
        # Positional: set(key, value, ttl=...) or set(key, value, ttl=...)
        if c.args and len(c.args) >= 2 and isinstance(c.args[1], ChunkJobStatus):
            statuses.append(c.args[1])
    return statuses


# ---------------------------------------------------------------------------
# 1. JSON-serializable job arguments
# ---------------------------------------------------------------------------


class TestJobArgsSerialization:
    """extraction_job(ctx, run_id, chunk_id, doc_id) — all args are plain strings.

    ARQ serializes job arguments to JSON when enqueuing. If any argument is not
    JSON-serializable, the job cannot be enqueued. This test verifies that
    the argument types match the documented contract (primitives only).
    """

    def test_run_id_is_json_serializable_string(self) -> None:
        assert json.dumps(_RUN_ID) == f'"{_RUN_ID}"'

    def test_chunk_id_is_json_serializable_string(self) -> None:
        assert json.dumps(_CHUNK_ID) == f'"{_CHUNK_ID}"'

    def test_doc_id_is_json_serializable_string(self) -> None:
        assert json.dumps(_DOC_ID) == f'"{_DOC_ID}"'

    def test_all_args_together_json_serializable(self) -> None:
        payload = {"run_id": _RUN_ID, "chunk_id": _CHUNK_ID, "doc_id": _DOC_ID}
        serialized = json.dumps(payload)
        recovered = json.loads(serialized)
        assert recovered == payload

    def test_arg_types_are_primitive_strings(self) -> None:
        for arg in (_RUN_ID, _CHUNK_ID, _DOC_ID):
            assert isinstance(arg, str), f"Expected str, got {type(arg)}"


# ---------------------------------------------------------------------------
# 2. ExtractionService failure — job must not raise
# ---------------------------------------------------------------------------


class TestExtractionServiceFailure:
    async def test_extraction_error_does_not_propagate(self) -> None:
        """ExtractionError is caught in the except block; job returns None."""
        with _PatchedJob() as p:
            p.extraction_svc.extract_chunk.side_effect = ExtractionError(
                "LLM returned invalid output"
            )
            # Must not raise — sibling chunks in the run must continue
            result = await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)
            assert result is None

    async def test_extraction_error_writes_failed_status(self) -> None:
        error_msg = "LLM returned invalid output for schema conformance"
        with _PatchedJob() as p:
            p.extraction_svc.extract_chunk.side_effect = ExtractionError(error_msg)
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)

            statuses = _status_set_calls(p.cache)
            failed = [s for s in statuses if s.status == "failed"]
            assert len(failed) == 1, "Expected exactly one status='failed' write"
            assert failed[0].run_id == _RUN_ID
            assert failed[0].chunk_id == _CHUNK_ID
            assert failed[0].doc_id == _DOC_ID
            assert error_msg in failed[0].error  # type: ignore[operator]

    async def test_extraction_error_does_not_write_complete_status(self) -> None:
        with _PatchedJob() as p:
            p.extraction_svc.extract_chunk.side_effect = ExtractionError("boom")
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)

            statuses = _status_set_calls(p.cache)
            complete = [s for s in statuses if s.status == "complete"]
            assert len(complete) == 0

    async def test_running_status_written_before_failure(self) -> None:
        """status='running' is written before extraction starts."""
        with _PatchedJob() as p:
            p.extraction_svc.extract_chunk.side_effect = ExtractionError("fail")
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)

            statuses = _status_set_calls(p.cache)
            # First ChunkJobStatus write must be 'running'
            assert statuses[0].status == "running"
            # Last must be 'failed'
            assert statuses[-1].status == "failed"

    async def test_unexpected_exception_also_caught(self) -> None:
        """Any exception (not just ExtractionError) must be swallowed."""
        with _PatchedJob() as p:
            p.extraction_svc.extract_chunk.side_effect = RuntimeError(
                "Unexpected internal error"
            )
            result = await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)
            assert result is None

        with _PatchedJob() as p:
            p.extraction_svc.extract_chunk.side_effect = RuntimeError("internal")
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)
            statuses = _status_set_calls(p.cache)
            assert any(s.status == "failed" for s in statuses)


# ---------------------------------------------------------------------------
# 3. GraphWriter failure — job must not raise
# ---------------------------------------------------------------------------


class TestGraphWriterFailure:
    async def test_graph_write_error_does_not_propagate(self) -> None:
        with _PatchedJob() as p:
            p.extraction_svc.extract_chunk.return_value = _make_extraction_result()
            p.writer_instance.write_extraction_result.side_effect = RuntimeError(
                "Neo4j connection dropped"
            )
            result = await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)
            assert result is None

    async def test_graph_write_error_writes_failed_status(self) -> None:
        error_msg = "Neo4j connection dropped mid-write"
        with _PatchedJob() as p:
            p.extraction_svc.extract_chunk.return_value = _make_extraction_result()
            p.writer_instance.write_extraction_result.side_effect = RuntimeError(
                error_msg
            )
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)

            statuses = _status_set_calls(p.cache)
            failed = [s for s in statuses if s.status == "failed"]
            assert len(failed) == 1
            assert error_msg in failed[0].error  # type: ignore[operator]

    async def test_graph_write_error_does_not_write_complete(self) -> None:
        with _PatchedJob() as p:
            p.extraction_svc.extract_chunk.return_value = _make_extraction_result()
            p.writer_instance.write_extraction_result.side_effect = RuntimeError(
                "write failed"
            )
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)

            statuses = _status_set_calls(p.cache)
            assert not any(s.status == "complete" for s in statuses)

    async def test_extraction_service_was_called_before_graph_write(self) -> None:
        """Verify ordering: extraction must succeed before graph write is attempted."""
        with _PatchedJob() as p:
            p.extraction_svc.extract_chunk.return_value = _make_extraction_result()
            p.writer_instance.write_extraction_result.side_effect = RuntimeError(
                "write failed"
            )
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)
            p.extraction_svc.extract_chunk.assert_called_once()
            p.writer_instance.write_extraction_result.assert_called_once()


# ---------------------------------------------------------------------------
# 4. Successful job path — status='complete' written with result counts
# ---------------------------------------------------------------------------


class TestSuccessfulJob:
    async def test_complete_status_written_on_success(self) -> None:
        with _PatchedJob() as p:
            p.extraction_svc.extract_chunk.return_value = _make_extraction_result()
            p.writer_instance.write_extraction_result.return_value = _make_write_result(
                nodes_created=2, edges_created=1
            )
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)

            statuses = _status_set_calls(p.cache)
            complete = [s for s in statuses if s.status == "complete"]
            assert len(complete) == 1
            assert complete[0].nodes_created == 2
            assert complete[0].edges_created == 1

    async def test_graph_writer_receives_extraction_result(self) -> None:
        with _PatchedJob() as p:
            extraction_result = _make_extraction_result()
            p.extraction_svc.extract_chunk.return_value = extraction_result
            p.writer_instance.write_extraction_result.return_value = _make_write_result()
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)

            p.writer_instance.write_extraction_result.assert_called_once_with(
                extraction_result
            )


# ---------------------------------------------------------------------------
# 5. Idempotency — already-complete job is skipped
# ---------------------------------------------------------------------------


class TestOperationalIdempotency:
    async def test_complete_job_skipped_no_extraction(self) -> None:
        """If status='complete' is in Redis, extraction must not run again."""
        existing_complete = ChunkJobStatus(
            run_id=_RUN_ID,
            chunk_id=_CHUNK_ID,
            doc_id=_DOC_ID,
            status="complete",
        )
        with _PatchedJob() as p:
            # First get() call returns the complete status → job skips immediately
            p.cache.get.return_value = existing_complete
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)

            p.extraction_svc.extract_chunk.assert_not_called()
            p.writer_instance.write_extraction_result.assert_not_called()

    async def test_complete_job_skipped_no_new_redis_write(self) -> None:
        existing_complete = ChunkJobStatus(
            run_id=_RUN_ID,
            chunk_id=_CHUNK_ID,
            doc_id=_DOC_ID,
            status="complete",
        )
        with _PatchedJob() as p:
            p.cache.get.return_value = existing_complete
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)
            # No new status should have been persisted
            p.cache.set.assert_not_called()

    async def test_chunk_text_cached_then_reused(self) -> None:
        """Cache hit for chunk text bypasses Qdrant retrieval."""
        from api.worker.jobs import _CachedChunkText  # type: ignore[attr-defined]  # noqa: PLC0415

        cached_text = _CachedChunkText(text=_CHUNK_TEXT)
        with _PatchedJob() as p:
            # Return cached text on chunk key lookup, None for job status
            def _get_side_effect(key: str, *, model: type) -> object:
                if "chunk:" in key:
                    return cached_text
                return None

            p.cache.get.side_effect = _get_side_effect
            p.extraction_svc.extract_chunk.return_value = _make_extraction_result()
            p.writer_instance.write_extraction_result.return_value = _make_write_result()
            await extraction_job(_make_ctx(), _RUN_ID, _CHUNK_ID, _DOC_ID)

            # Qdrant should not be consulted when the cache has the text
            p.indexer.get_chunk_text.assert_not_called()
