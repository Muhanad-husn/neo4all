"""
api/routers/extraction.py — POST /api/extraction/run,
GET /api/extraction/status/{run_id}, GET /api/extraction/results/{run_id}
(SPEC-04 S-04.5).

Phase 3 extraction lifecycle:
  POST /run     — Check schema is locked, list all chunks from manifests, enqueue
                  one ARQ job per pending chunk.  Returns 409 if schema not locked.
  GET /status   — Aggregate per-chunk job statuses from Redis into a run summary.
  GET /results  — Return Neo4j entity counts for a run (cached, parameterized Cypher).

Architecture (SKILL-B: thin routers)
--------------------------------------
All I/O is delegated to the cache client, ArtifactsService, Neo4jClient, and the
ARQ pool.  No business logic lives inside the route handler functions — handlers
coordinate calls, map errors to HTTP status codes, and build response models.

Request/response models (SKILL-A)
----------------------------------
Models are co-located in this module per project guidance for SPEC-04 S-04.5.
All response models extend BaseResponse (SKILL-A R-A2).  No raw dicts cross
module boundaries (SKILL-A R-A3).

Error handling
--------------
POST /run:
  409 — schema not locked for this run_id (approve Phase 1 first).
  503 — S3 unavailable, manifest list cannot be retrieved.
GET /status:
  503 — S3 unavailable, cannot establish total chunk set.
GET /results:
  Always HTTP 200.  Returns zeros if nothing has been extracted or Neo4j is
  temporarily unavailable (fail-open on read-only monitoring).

Path ordering
-------------
Static sub-paths (/run) must be registered before parameterised paths
(/status/{run_id}, /results/{run_id}) to prevent FastAPI spurious path matches.

Caching (SKILL-D R-D8)
-----------------------
GET /results reads graph-query cache first:
  Key: CacheKey.graph_query(run_id, _COUNTS_QUERY_HASH)
  TTL: 300 s (5 minutes — graph counts change as jobs complete)

Sensitive data (SKILL-D R-D5)
------------------------------
REDIS_URL is never logged in full.  chunk_id (a hash) is safe to log.
chunk_text is never referenced here.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.common.arq_pool import get_arq_pool
from api.graph.client import Neo4jClient, get_neo4j_client
from api.models.responses import BaseResponse, ErrorDetail
from api.observability.correlation import get_correlation_id
from api.observability.logger import get_logger
from api.schema.models import SchemaVersion
from api.storage.artifacts import ArtifactsService, StorageError, get_artifacts_service
from api.worker.jobs import ChunkJobStatus

logger = get_logger(__name__)

router = APIRouter(tags=["extraction"])

_JOB_STATUS_TTL_S: int = 86400   # 24 h — mirrors api/worker/jobs._JOB_STATUS_TTL_S


# ---------------------------------------------------------------------------
# Graph read helper — parameterized Cypher, cached (SKILL-D R-D8)
# ---------------------------------------------------------------------------

# Deterministic hash that identifies the "extraction entity counts" query.
# Used as the query_hash component of CacheKey.graph_query().
_COUNTS_QUERY_HASH: str = hashlib.sha256(b"extraction_run_entity_counts_v1").hexdigest()[:16]


class _RunEntityCounts(BaseModel):
    """Cache envelope for Neo4j node/edge count query results.

    CacheClient requires a BaseModel subclass for typed serialisation.
    """

    node_count: int
    edge_count: int


async def _get_entity_counts(run_id: str, neo4j: Neo4jClient) -> _RunEntityCounts:
    """Return node and edge counts for a run from Neo4j, cache-first.

    Queries Neo4j with parameterized Cypher only (CLAUDE.md §4.2).
    Results are cached under CacheKey.graph_query with a 5-minute TTL per
    SKILL-D R-D8.  Falls back to zeros on any Neo4j or cache error — this
    is a monitoring endpoint and must not fail closed (SKILL-D R-D9).

    Args:
        run_id: Governed run identifier.
        neo4j:  Open Neo4jClient (injected via Depends in the route handler).

    Returns:
        _RunEntityCounts with node_count and edge_count. Never raises.

    Log events:
        extraction_counts_cache_hit   DEBUG — run_id
        extraction_counts_queried     DEBUG — run_id, node_count, edge_count
        extraction_counts_query_failed ERROR — run_id, error
    """
    cache = get_cache_client()
    cache_key = CacheKey.graph_query(run_id=run_id, query_hash=_COUNTS_QUERY_HASH)

    cached = await cache.get(cache_key, model=_RunEntityCounts)
    if cached is not None:
        logger.debug("extraction_counts_cache_hit", run_id=run_id)
        return cached

    try:
        # Closures capture run_id from the outer function parameter — stable.
        async def _count_nodes(tx: Any) -> int:
            result = await tx.run(
                "MATCH (n) WHERE n._run_id = $run_id RETURN count(n) AS c",
                run_id=run_id,
            )
            record = await result.single()
            return int(record["c"]) if record else 0

        async def _count_edges(tx: Any) -> int:
            result = await tx.run(
                "MATCH ()-[r]->() WHERE r._run_id = $run_id RETURN count(r) AS c",
                run_id=run_id,
            )
            record = await result.single()
            return int(record["c"]) if record else 0

        async with neo4j.acquire_session() as session:
            node_count = await session.execute_read(_count_nodes)
            edge_count = await session.execute_read(_count_edges)

        counts = _RunEntityCounts(node_count=node_count, edge_count=edge_count)
        await cache.set(cache_key, counts, ttl=300)

        logger.debug(
            "extraction_counts_queried",
            run_id=run_id,
            node_count=node_count,
            edge_count=edge_count,
        )
        return counts

    except Exception as exc:
        logger.error(
            "extraction_counts_query_failed",
            run_id=run_id,
            error=str(exc),
        )
        return _RunEntityCounts(node_count=0, edge_count=0)


# ---------------------------------------------------------------------------
# Request / Response models (SKILL-A R-A1, R-A2)
# ---------------------------------------------------------------------------


class ExtractionRunRequest(BaseModel):
    """Request body for POST /api/extraction/run."""

    run_id: str


class ExtractionRunResponse(BaseResponse):
    """Response for POST /api/extraction/run.

    jobs_enqueued is 0 when all chunks are already complete (idempotent re-call).
    """

    jobs_enqueued: int = 0


class ExtractionStatusResponse(BaseResponse):
    """Aggregated per-run extraction job status, derived from Redis.

    Attributes:
        total_chunks: Total number of chunks across all documents in the run
                      (derived from DocumentManifest.chunk_ids).
        completed:    Chunks whose job status is "complete" in Redis.
        failed:       Chunks whose job status is "failed" in Redis.
        pending:      Remaining chunks — queued, running, or not yet enqueued.
    """

    total_chunks: int = 0
    completed: int = 0
    failed: int = 0
    pending: int = 0


class ExtractionResultsResponse(BaseResponse):
    """Extracted entity counts from Neo4j for a run.

    Attributes:
        node_count:     Total nodes written to Neo4j with _run_id matching this run.
        edge_count:     Total relationships written to Neo4j with this run's _run_id.
        schema_version: version_hash of the locked schema; empty string if not yet locked.
    """

    node_count: int = 0
    edge_count: int = 0
    schema_version: str = ""


# ---------------------------------------------------------------------------
# POST /api/extraction/run
# ---------------------------------------------------------------------------


@router.post(
    "/run",
    response_model=ExtractionRunResponse,
    summary="Enqueue ARQ extraction jobs for all pending chunks in a run",
    responses={
        409: {
            "model": ExtractionRunResponse,
            "description": "Schema not locked — approve Phase 1 before running extraction",
        },
        503: {
            "model": ExtractionRunResponse,
            "description": "S3 unavailable — cannot retrieve manifest list",
        },
    },
)
async def run_extraction(
    request: ExtractionRunRequest,
    response: Response,
    artifacts: ArtifactsService = Depends(get_artifacts_service),
) -> ExtractionRunResponse:
    """Enqueue one ARQ extraction job per pending chunk for a run.

    Requires the domain schema to be locked (Phase 1 complete).  Returns 409
    Conflict if the schema has not been approved for this run_id.

    Idempotent: chunks whose Redis job status is already ``"complete"`` are
    skipped; a ``"queued"`` status is written to Redis for all other chunks
    before enqueueing so that ``GET /status`` can see them immediately.

    **Errors**
    - ``409 Conflict`` — no locked schema found for this run_id.
    - ``503 Service Unavailable`` — S3 unavailable; manifest list failed.
    """
    logger.info("extraction_run_requested", run_id=request.run_id)

    # ------------------------------------------------------------------
    # 1. Verify schema is locked (CLAUDE.md §4.4: fail-closed on missing schema)
    # ------------------------------------------------------------------
    cache = get_cache_client()
    schema: SchemaVersion | None = await cache.get(
        CacheKey.schema(run_id=request.run_id),
        model=SchemaVersion,
    )
    if schema is None:
        logger.warning("extraction_schema_not_locked", run_id=request.run_id)
        response.status_code = 409
        return ExtractionRunResponse(
            run_id=request.run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="schema_not_locked",
                    message=(
                        f"No locked schema found for run '{request.run_id}'. "
                        "Complete Phase 1 (approve the domain schema) before "
                        "running extraction."
                    ),
                )
            ],
        )

    # ------------------------------------------------------------------
    # 2. List all manifests to build the complete chunk set
    # ------------------------------------------------------------------
    try:
        manifests = await artifacts.list_manifests_for_run(request.run_id)
    except StorageError as exc:
        logger.error(
            "extraction_run_storage_error",
            run_id=request.run_id,
            error=exc.message,
        )
        response.status_code = 503
        return ExtractionRunResponse(
            run_id=request.run_id,
            status="error",
            errors=[ErrorDetail(code=exc.code, message=exc.message)],
        )

    # ------------------------------------------------------------------
    # 3. Enqueue jobs for all non-complete chunks
    # ------------------------------------------------------------------
    arq_pool = await get_arq_pool()
    jobs_enqueued = 0

    for manifest in manifests:
        for chunk_id in manifest.chunk_ids:
            existing: ChunkJobStatus | None = await cache.get(
                CacheKey.job_status(run_id=request.run_id, chunk_id=chunk_id),
                model=ChunkJobStatus,
            )
            if existing is not None and existing.status == "complete":
                logger.debug(
                    "extraction_chunk_skip_complete",
                    run_id=request.run_id,
                    chunk_id=chunk_id,
                )
                continue

            # Write "queued" status before enqueueing so GET /status sees it
            # immediately, even before the worker picks up the job.
            await cache.set(
                CacheKey.job_status(run_id=request.run_id, chunk_id=chunk_id),
                ChunkJobStatus(
                    run_id=request.run_id,
                    chunk_id=chunk_id,
                    doc_id=manifest.doc_id,
                    status="queued",
                ),
                ttl=_JOB_STATUS_TTL_S,
            )

            await arq_pool.enqueue_job(
                "extraction_job",
                run_id=request.run_id,
                chunk_id=chunk_id,
                doc_id=manifest.doc_id,
                correlation_id=get_correlation_id(),
            )
            jobs_enqueued += 1

            logger.debug(
                "extraction_chunk_enqueued",
                run_id=request.run_id,
                chunk_id=chunk_id,
                doc_id=manifest.doc_id,
            )

    logger.info(
        "extraction_run_enqueued",
        run_id=request.run_id,
        jobs_enqueued=jobs_enqueued,
        total_manifests=len(manifests),
        schema_version=schema.version_hash,
    )

    return ExtractionRunResponse(
        run_id=request.run_id,
        status="success",
        jobs_enqueued=jobs_enqueued,
    )


# ---------------------------------------------------------------------------
# GET /api/extraction/status/{run_id}
# ---------------------------------------------------------------------------


@router.get(
    "/status/{run_id}",
    response_model=ExtractionStatusResponse,
    summary="Return per-run extraction job status aggregated from Redis",
    responses={
        503: {
            "model": ExtractionStatusResponse,
            "description": "S3 unavailable — cannot determine total chunk count",
        },
    },
)
async def get_extraction_status(
    run_id: str,
    response: Response,
    artifacts: ArtifactsService = Depends(get_artifacts_service),
) -> ExtractionStatusResponse:
    """Return aggregated extraction job status for all chunks in a run.

    Derives the total chunk set from DocumentManifests stored in S3/Redis,
    then reads individual per-chunk job statuses from Redis.  Chunks with no
    Redis entry (not yet enqueued) count toward ``pending``.

    Status breakdown:
    - **completed** — chunks with Redis status ``"complete"``.
    - **failed**    — chunks with Redis status ``"failed"``.
    - **pending**   — all others (``"queued"``, ``"running"``, or no entry).

    Returns HTTP 503 if the S3 manifest listing fails.
    """
    logger.info("extraction_status_requested", run_id=run_id)

    # --- Build the full chunk set from manifests (S3/Redis cache) ---
    try:
        manifests = await artifacts.list_manifests_for_run(run_id)
    except StorageError as exc:
        logger.error(
            "extraction_status_storage_error",
            run_id=run_id,
            error=exc.message,
        )
        response.status_code = 503
        return ExtractionStatusResponse(
            run_id=run_id,
            status="error",
            errors=[ErrorDetail(code=exc.code, message=exc.message)],
        )

    all_chunk_ids: list[str] = [
        chunk_id
        for m in manifests
        for chunk_id in m.chunk_ids
    ]
    total_chunks = len(all_chunk_ids)

    if total_chunks == 0:
        logger.debug("extraction_status_no_chunks", run_id=run_id)
        return ExtractionStatusResponse(
            run_id=run_id,
            status="success",
            total_chunks=0,
        )

    # --- Count statuses from Redis ---
    cache = get_cache_client()
    completed = 0
    failed = 0

    for chunk_id in all_chunk_ids:
        job_status: ChunkJobStatus | None = await cache.get(
            CacheKey.job_status(run_id=run_id, chunk_id=chunk_id),
            model=ChunkJobStatus,
        )
        if job_status is None:
            continue  # not yet enqueued — counts toward pending
        if job_status.status == "complete":
            completed += 1
        elif job_status.status == "failed":
            failed += 1
        # "queued" and "running" fall through to pending

    pending = total_chunks - completed - failed

    logger.info(
        "extraction_status_success",
        run_id=run_id,
        total_chunks=total_chunks,
        completed=completed,
        failed=failed,
        pending=pending,
    )

    return ExtractionStatusResponse(
        run_id=run_id,
        status="success",
        total_chunks=total_chunks,
        completed=completed,
        failed=failed,
        pending=pending,
    )


# ---------------------------------------------------------------------------
# GET /api/extraction/results/{run_id}
# ---------------------------------------------------------------------------


@router.get(
    "/results/{run_id}",
    response_model=ExtractionResultsResponse,
    summary="Return extracted entity counts from Neo4j for a run",
)
async def get_extraction_results(
    run_id: str,
    neo4j: Neo4jClient = Depends(get_neo4j_client),
) -> ExtractionResultsResponse:
    """Return a summary of entities extracted to Neo4j for a run.

    Queries Neo4j for the count of nodes and relationships whose ``_run_id``
    property matches this run.  Results are cached for 5 minutes
    (SKILL-D R-D8 graph-query cache).

    The ``schema_version`` field is populated from the locked schema in
    Redis; it is an empty string if the schema has not yet been approved.

    Always returns HTTP 200. Zeros are returned if nothing has been extracted
    yet, or if Neo4j is temporarily unavailable (monitoring path: fail-open).
    """
    logger.info("extraction_results_requested", run_id=run_id)

    # --- schema_version from cache (no Neo4j query needed) ---
    cache = get_cache_client()
    schema: SchemaVersion | None = await cache.get(
        CacheKey.schema(run_id=run_id),
        model=SchemaVersion,
    )
    schema_version = schema.version_hash if schema is not None else ""

    # --- Entity counts from Neo4j, cached 5 min (SKILL-D R-D8) ---
    counts = await _get_entity_counts(run_id=run_id, neo4j=neo4j)

    logger.info(
        "extraction_results_success",
        run_id=run_id,
        node_count=counts.node_count,
        edge_count=counts.edge_count,
        schema_version=schema_version,
    )

    return ExtractionResultsResponse(
        run_id=run_id,
        status="success",
        node_count=counts.node_count,
        edge_count=counts.edge_count,
        schema_version=schema_version,
    )
