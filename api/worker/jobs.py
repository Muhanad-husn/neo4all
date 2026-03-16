"""
api/worker/jobs.py — ARQ jobs: extraction (SPEC-04).

Extraction jobs (SPEC-04 S-04.1)
---------------------------------
extraction_job(ctx, run_id, chunk_id, doc_id)
  Per-chunk extraction: idempotency check -> retrieve chunk -> LLM extraction
  -> Neo4j write -> mark complete.

batch_extraction_job(ctx, run_id, chunk_ids_json, doc_ids_json)
  Batch extraction: process multiple chunks in a single LLM call.  Fetches
  all chunk texts, calls ExtractionService.extract_batch(), writes each
  result to Neo4j independently.  Per-chunk failure handling: each chunk
  gets its own job status — one failure does not block siblings.

Job arguments
-------------
Extraction: run_id (str), chunk_id (str), doc_id (str), correlation_id (str).
Batch extraction: run_id (str), chunk_ids_json (str), doc_ids_json (str),
  correlation_id (str).  JSON-serialised lists because ARQ serialises
  positional args — lists would work but JSON strings are explicit.

All jobs accept an optional ``correlation_id`` parameter (SKILL-D R-D2).
When non-empty, the ID is bound to structlog's context at job start so
every log entry within the job carries the same correlation ID as the
originating HTTP request.

Sensitive data (SKILL-D R-D5)
------------------------------
  chunk.text is passed to services but never logged.
  Only chunk_id (safe hashes) appear in log entries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.graph.client import Neo4jClient
from api.graph.writer import GraphWriter
from api.observability.correlation import set_correlation_id
from api.observability.logger import get_logger
from api.services.extraction import ExtractionError, ExtractionResult, get_extraction_service
from api.vector.indexer import get_vector_indexer
from api.worker.config_sync import sync_credentials_from_redis

logger = get_logger(__name__)

_JOB_STATUS_TTL_S: int = 86400  # 24 hours
_CHUNK_TEXT_TTL_S: int = 1800   # 30 minutes per SKILL-D R-D8


# ---------------------------------------------------------------------------
# Public: per-chunk job status model
# ---------------------------------------------------------------------------


class ChunkJobStatus(BaseModel):
    """Redis-persisted status for a single chunk extraction job.

    Written by extraction_job at each lifecycle transition; read by
    api/routers/monitoring.py (GET /api/monitoring/jobs/{run_id}).

    Attributes:
        run_id:        Governed run this job belongs to.
        chunk_id:      Target chunk identifier (safe to log — never raw text).
        doc_id:        Parent document identifier.
        status:        Job lifecycle state.
        error:         Error message set on "failed"; None otherwise.
        nodes_created: Nodes created in Neo4j (populated on "complete").
        nodes_matched: Nodes matched (de-duped) in Neo4j.
        edges_created: Edges created in Neo4j.
        edges_matched: Edges matched (de-duped) in Neo4j.
        edges_skipped: Edges skipped due to absent endpoint nodes.
        started_at:    ISO 8601 UTC timestamp of job_start event.
        completed_at:  ISO 8601 UTC timestamp of completion or failure.
    """

    run_id: str
    chunk_id: str
    doc_id: str
    status: Literal["queued", "running", "complete", "failed", "cancelled"]
    error: str | None = None
    nodes_created: int = 0
    nodes_matched: int = 0
    edges_created: int = 0
    edges_matched: int = 0
    edges_skipped: int = 0
    started_at: str | None = None
    completed_at: str | None = None


# ---------------------------------------------------------------------------
# Private: chunk text cache wrapper
# ---------------------------------------------------------------------------


class _CachedChunkText(BaseModel):
    """Internal Pydantic wrapper so CacheClient can serialize a plain string.

    CacheClient.get/set requires a BaseModel subclass. This thin wrapper lets
    us store and retrieve chunk text under CacheKey.chunk(chunk_id) without
    extending CacheClient with raw-string methods.
    """

    text: str


# ---------------------------------------------------------------------------
# Private: job status helpers
# ---------------------------------------------------------------------------


async def _set_job_status(status: ChunkJobStatus) -> None:
    """Write ChunkJobStatus to Redis (TTL 24 h).

    Cache failure is logged at WARNING by CacheClient and ignored here —
    status persistence is best-effort; it does not affect job correctness.
    """
    cache = get_cache_client()
    await cache.set(
        CacheKey.job_status(run_id=status.run_id, chunk_id=status.chunk_id),
        status,
        ttl=_JOB_STATUS_TTL_S,
    )


async def _get_job_status(run_id: str, chunk_id: str) -> ChunkJobStatus | None:
    """Read ChunkJobStatus from Redis. Returns None on cache miss or error."""
    cache = get_cache_client()
    return await cache.get(
        CacheKey.job_status(run_id=run_id, chunk_id=chunk_id),
        model=ChunkJobStatus,
    )


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


async def extraction_job(
    ctx: dict,
    run_id: str,
    chunk_id: str,
    doc_id: str,
    correlation_id: str = "",
    model_override: str | None = None,
) -> None:
    """Extract entities from one chunk and write them to Neo4j.

    Args:
        ctx:            ARQ worker context dict. Must contain "neo4j_client"
                        (an open Neo4jClient injected by WorkerSettings.on_startup).
        run_id:         Governed run identifier. The locked schema for this run must
                        be in Redis (approved in Phase 1) before enqueuing jobs.
        chunk_id:       Target chunk identifier (Chunk.chunk_id — SHA-256 hex).
        doc_id:         Parent document identifier; stored in ChunkJobStatus for
                        per-job monitoring visibility.
        correlation_id: Propagated from the HTTP request that enqueued this job
                        (SKILL-D R-D2).  Bound to structlog context so all log
                        entries within this job carry the same correlation ID.
        model_override: Optional OpenRouter model ID.  When provided, overrides
                        the default extraction model for this job only.

    Returns:
        None. Results are persisted to Neo4j; status is persisted to Redis.

    Raises:
        Nothing. All exceptions are caught, logged at ERROR, and translated
        to status="failed" so that sibling chunks in the run continue.

    Log events:
        job_start                      INFO  — run_id, chunk_id, doc_id
        job_skipped_already_complete   INFO  — run_id, chunk_id
        chunk_cache_hit                DEBUG — run_id, chunk_id
        chunk_not_found                ERROR — run_id, chunk_id, doc_id
        extraction_complete            INFO  — run_id, chunk_id, node_count, edge_count
        graph_write_complete           INFO  — run_id, chunk_id, all write counts
        job_failed                     ERROR — run_id, chunk_id, doc_id, error
    """
    # Bind correlation ID to this worker task's structlog context (SKILL-D R-D2).
    if correlation_id:
        set_correlation_id(correlation_id)

    # Sync credentials from Redis in case the API received updated creds
    # from the UI after this worker process started.
    await sync_credentials_from_redis(ctx=ctx)

    started_at = datetime.now(UTC).isoformat()

    # ------------------------------------------------------------------
    # 1. Operational idempotency check
    # ------------------------------------------------------------------
    existing = await _get_job_status(run_id=run_id, chunk_id=chunk_id)
    if existing is not None and existing.status == "complete":
        logger.info(
            "job_skipped_already_complete",
            run_id=run_id,
            chunk_id=chunk_id,
        )
        return

    # ------------------------------------------------------------------
    # 2. Mark running
    # ------------------------------------------------------------------
    await _set_job_status(
        ChunkJobStatus(
            run_id=run_id,
            chunk_id=chunk_id,
            doc_id=doc_id,
            status="running",
            started_at=started_at,
        )
    )
    logger.info("job_start", run_id=run_id, chunk_id=chunk_id, doc_id=doc_id)

    try:
        # ------------------------------------------------------------------
        # 3. Retrieve chunk text — cache-aside (SKILL-D R-D8)
        # ------------------------------------------------------------------
        cache = get_cache_client()

        cached_text = await cache.get(CacheKey.chunk(chunk_id), model=_CachedChunkText)
        if cached_text is not None:
            chunk_text: str = cached_text.text
            logger.debug("chunk_cache_hit", run_id=run_id, chunk_id=chunk_id)
        else:
            # Cache miss — retrieve from Qdrant (evidence-only read path).
            indexer = get_vector_indexer()
            raw_text = await indexer.get_chunk_text(run_id=run_id, chunk_id=chunk_id)
            if raw_text is None:
                logger.error(
                    "chunk_not_found",
                    run_id=run_id,
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                )
                raise ExtractionError(
                    f"Chunk text not found for chunk_id='{chunk_id}' in run='{run_id}'. "
                    "Ensure the chunk was indexed in Qdrant before enqueuing extraction."
                )
            # Populate cache — chunks are immutable, 30-min TTL sufficient.
            await cache.set(
                CacheKey.chunk(chunk_id),
                _CachedChunkText(text=raw_text),
                ttl=_CHUNK_TEXT_TTL_S,
            )
            chunk_text = raw_text

        # ------------------------------------------------------------------
        # 4. LLM extraction (fail-closed: ExtractionError on schema mismatch
        #    or malformed LLM output — CLAUDE.md §4.4)
        # ------------------------------------------------------------------
        extraction_svc = get_extraction_service()
        result = await extraction_svc.extract_chunk(
            run_id=run_id,
            chunk_id=chunk_id,
            chunk_text=chunk_text,  # never logged (SKILL-D R-D5)
            model_override=model_override,
        )
        logger.info(
            "extraction_complete",
            run_id=run_id,
            chunk_id=chunk_id,
            node_count=len(result.nodes),
            edge_count=len(result.edges),
        )

        # ------------------------------------------------------------------
        # 5. Write to Neo4j via GraphWriter
        #    Structural idempotency: MERGE on node_dedupe_key / rel_dedupe_key
        #    guarantees no duplicate nodes or edges on re-run.
        # ------------------------------------------------------------------
        neo4j_client: Neo4jClient = ctx["neo4j_client"]
        writer = GraphWriter(neo4j_client)
        write_result = await writer.write_extraction_result(result)
        logger.info(
            "graph_write_complete",
            run_id=run_id,
            chunk_id=chunk_id,
            nodes_created=write_result.nodes_created,
            nodes_matched=write_result.nodes_matched,
            edges_created=write_result.edges_created,
            edges_matched=write_result.edges_matched,
            edges_skipped=write_result.edges_skipped,
        )

        # ------------------------------------------------------------------
        # 6. Mark complete
        # ------------------------------------------------------------------
        completed_at = datetime.now(UTC).isoformat()
        await _set_job_status(
            ChunkJobStatus(
                run_id=run_id,
                chunk_id=chunk_id,
                doc_id=doc_id,
                status="complete",
                nodes_created=write_result.nodes_created,
                nodes_matched=write_result.nodes_matched,
                edges_created=write_result.edges_created,
                edges_matched=write_result.edges_matched,
                edges_skipped=write_result.edges_skipped,
                started_at=started_at,
                completed_at=completed_at,
            )
        )

    except BaseException as exc:
        # ------------------------------------------------------------------
        # Per-chunk failure: persist FAILED status, log ERROR, do NOT raise.
        # Sibling chunk jobs for the same run must continue unaffected.
        # Catches BaseException (not just Exception) so that ARQ job
        # timeouts — which surface as asyncio.CancelledError, a
        # BaseException subclass — also mark the job as failed.
        # ------------------------------------------------------------------
        error_msg = str(exc) or type(exc).__name__
        completed_at = datetime.now(UTC).isoformat()
        try:
            await _set_job_status(
                ChunkJobStatus(
                    run_id=run_id,
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    status="failed",
                    error=error_msg,
                    started_at=started_at,
                    completed_at=completed_at,
                )
            )
        except Exception:
            pass  # best-effort status write during cancellation
        logger.error(
            "job_failed",
            run_id=run_id,
            chunk_id=chunk_id,
            doc_id=doc_id,
            error=error_msg,
        )


# ---------------------------------------------------------------------------
# Batch extraction job
# ---------------------------------------------------------------------------


async def batch_extraction_job(
    ctx: dict,
    run_id: str,
    chunk_ids_json: str,
    doc_ids_json: str,
    correlation_id: str = "",
    model_override: str | None = None,
) -> None:
    """Extract entities from multiple chunks in a single LLM call.

    Groups chunks into one batch prompt, calls ExtractionService.extract_batch(),
    then writes each chunk's results to Neo4j independently.  Per-chunk failure
    is isolated — one bad chunk does not block siblings.

    Args:
        ctx:             ARQ worker context (must contain "neo4j_client").
        run_id:          Governed run identifier.
        chunk_ids_json:  JSON-serialised list of chunk_id strings.
        doc_ids_json:    JSON-serialised list of doc_id strings (parallel to chunk_ids).
        correlation_id:  Propagated from the HTTP request (SKILL-D R-D2).
        model_override:  Optional OpenRouter model override.

    Returns:
        None. Results persisted to Neo4j; per-chunk statuses persisted to Redis.

    Raises:
        Nothing. All exceptions caught and translated to per-chunk "failed" statuses.
    """
    import json as _json

    if correlation_id:
        set_correlation_id(correlation_id)

    await sync_credentials_from_redis(ctx=ctx)

    chunk_ids: list[str] = _json.loads(chunk_ids_json)
    doc_ids: list[str] = _json.loads(doc_ids_json)
    chunk_to_doc: dict[str, str] = dict(zip(chunk_ids, doc_ids))

    started_at = datetime.now(UTC).isoformat()
    batch_size = len(chunk_ids)

    logger.info(
        "batch_extraction_start",
        run_id=run_id,
        batch_size=batch_size,
    )

    # Mark all chunks as running.
    for chunk_id in chunk_ids:
        await _set_job_status(
            ChunkJobStatus(
                run_id=run_id,
                chunk_id=chunk_id,
                doc_id=chunk_to_doc[chunk_id],
                status="running",
                started_at=started_at,
            )
        )

    try:
        # ------------------------------------------------------------------
        # 1. Retrieve chunk texts — cache-aside (SKILL-D R-D8)
        # ------------------------------------------------------------------
        cache = get_cache_client()
        indexer = get_vector_indexer()
        chunks: list[tuple[str, str]] = []  # (chunk_id, chunk_text)
        missing_chunks: list[str] = []

        for chunk_id in chunk_ids:
            cached_text = await cache.get(
                CacheKey.chunk(chunk_id), model=_CachedChunkText
            )
            if cached_text is not None:
                chunks.append((chunk_id, cached_text.text))
                logger.debug("chunk_cache_hit", run_id=run_id, chunk_id=chunk_id)
            else:
                raw_text = await indexer.get_chunk_text(
                    run_id=run_id, chunk_id=chunk_id
                )
                if raw_text is None:
                    logger.error(
                        "chunk_not_found",
                        run_id=run_id,
                        chunk_id=chunk_id,
                        doc_id=chunk_to_doc[chunk_id],
                    )
                    missing_chunks.append(chunk_id)
                    continue
                await cache.set(
                    CacheKey.chunk(chunk_id),
                    _CachedChunkText(text=raw_text),
                    ttl=_CHUNK_TEXT_TTL_S,
                )
                chunks.append((chunk_id, raw_text))

        # Mark missing chunks as failed immediately.
        for chunk_id in missing_chunks:
            await _set_job_status(
                ChunkJobStatus(
                    run_id=run_id,
                    chunk_id=chunk_id,
                    doc_id=chunk_to_doc[chunk_id],
                    status="failed",
                    error=f"Chunk text not found for chunk_id='{chunk_id}'",
                    started_at=started_at,
                    completed_at=datetime.now(UTC).isoformat(),
                )
            )

        if not chunks:
            logger.error(
                "batch_extraction_no_chunks",
                run_id=run_id,
                batch_size=batch_size,
            )
            return

        # ------------------------------------------------------------------
        # 2. Batch LLM extraction
        # ------------------------------------------------------------------
        extraction_svc = get_extraction_service()
        results: dict[str, ExtractionResult] = await extraction_svc.extract_batch(
            run_id=run_id,
            chunks=chunks,
            model_override=model_override,
        )

        # ------------------------------------------------------------------
        # 3. Write results to Neo4j and update statuses
        # ------------------------------------------------------------------
        neo4j_client: Neo4jClient = ctx["neo4j_client"]
        writer = GraphWriter(neo4j_client)

        for chunk_id, _ in chunks:
            result = results.get(chunk_id)
            if result is None:
                # LLM didn't return results for this chunk or validation failed.
                await _set_job_status(
                    ChunkJobStatus(
                        run_id=run_id,
                        chunk_id=chunk_id,
                        doc_id=chunk_to_doc[chunk_id],
                        status="failed",
                        error="Batch extraction returned no valid result for this chunk",
                        started_at=started_at,
                        completed_at=datetime.now(UTC).isoformat(),
                    )
                )
                continue

            try:
                write_result = await writer.write_extraction_result(result)
                logger.info(
                    "graph_write_complete",
                    run_id=run_id,
                    chunk_id=chunk_id,
                    nodes_created=write_result.nodes_created,
                    nodes_matched=write_result.nodes_matched,
                    edges_created=write_result.edges_created,
                    edges_matched=write_result.edges_matched,
                    edges_skipped=write_result.edges_skipped,
                )
                await _set_job_status(
                    ChunkJobStatus(
                        run_id=run_id,
                        chunk_id=chunk_id,
                        doc_id=chunk_to_doc[chunk_id],
                        status="complete",
                        nodes_created=write_result.nodes_created,
                        nodes_matched=write_result.nodes_matched,
                        edges_created=write_result.edges_created,
                        edges_matched=write_result.edges_matched,
                        edges_skipped=write_result.edges_skipped,
                        started_at=started_at,
                        completed_at=datetime.now(UTC).isoformat(),
                    )
                )
            except Exception as exc:
                error_msg = str(exc) or type(exc).__name__
                logger.error(
                    "batch_extraction_chunk_write_failed",
                    run_id=run_id,
                    chunk_id=chunk_id,
                    error=error_msg,
                )
                await _set_job_status(
                    ChunkJobStatus(
                        run_id=run_id,
                        chunk_id=chunk_id,
                        doc_id=chunk_to_doc[chunk_id],
                        status="failed",
                        error=error_msg,
                        started_at=started_at,
                        completed_at=datetime.now(UTC).isoformat(),
                    )
                )

        logger.info(
            "batch_extraction_complete",
            run_id=run_id,
            batch_size=batch_size,
            succeeded=len(results),
        )

    except BaseException as exc:
        # Batch-level failure: mark all non-resolved chunks as failed.
        error_msg = str(exc) or type(exc).__name__
        completed_at = datetime.now(UTC).isoformat()
        for chunk_id in chunk_ids:
            try:
                existing = await _get_job_status(run_id=run_id, chunk_id=chunk_id)
                if existing is not None and existing.status in ("complete", "failed"):
                    continue  # already resolved
                await _set_job_status(
                    ChunkJobStatus(
                        run_id=run_id,
                        chunk_id=chunk_id,
                        doc_id=chunk_to_doc[chunk_id],
                        status="failed",
                        error=error_msg,
                        started_at=started_at,
                        completed_at=completed_at,
                    )
                )
            except Exception:
                pass  # best-effort
        logger.error(
            "batch_extraction_failed",
            run_id=run_id,
            batch_size=batch_size,
            error=error_msg,
        )
