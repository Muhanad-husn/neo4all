"""
api/vector/indexer.py — Chunk embedding and Qdrant indexing (SPEC-03 S-03.5).

Evidence-only: Qdrant holds chunk vectors and payload for retrieval during
Agent-B's evidence gathering. It is NEVER authoritative — Neo4j is the source
of truth (CLAUDE.md §4.1). No agent or service writes graph state to Qdrant.

Collection layout
-----------------
  Name:   chunks_{run_id}   (one collection per run — run-isolated)
  Vectors: dense floats, cosine distance, dimension from embedding model
  Payload (per point):
    chunk_id           str   — full 64-char SHA-256 hex (primary cross-ref)
    doc_id             str   — parent document identifier
    run_id             str   — governed run identifier
    start_page         int   — 0-indexed page number of chunk start
    end_page           int   — 0-indexed page number of chunk end
    start_page_locator str   — canonical locator "p{n}:c{i}"
    quality_flags      list  — ChunkQualityFlag string values (may be empty)
    text               str   — full chunk text for evidence display

Qdrant point IDs are deterministic UUIDs derived from the first 128 bits of
each chunk_id SHA-256 digest (see _chunk_id_to_point_id). The full chunk_id
is preserved in payload for exact cross-referencing.

Batching
--------
  Chunks are embedded and upserted in batches of UPSERT_BATCH_SIZE (100).
  Each batch is independent: an embedding or upsert failure in one batch is
  logged at ERROR and the remaining batches continue processing
  (SPEC-03: "don't halt entire ingestion").

Embedding
---------
  Default model: sentence-transformers/all-MiniLM-L6-v2 (384 dimensions).
  Configurable via EMBEDDING_MODEL in api/config.py.
  The model is loaded lazily on first use and reused for the service lifetime.
  Encoding is CPU-bound and synchronous; always runs inside asyncio.to_thread.

Sensitive data
--------------
  chunk.text IS written to Qdrant payload (required for evidence display).
  It is NOT written to logs (SKILL-D R-D5). Logs reference chunk_id only.

Connection
----------
  QDRANT_URL from config. When None (default), connects to localhost:6333
  (the docker-compose Qdrant container). Set for remote / Qdrant Cloud.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from api.models.document import Chunk
from api.observability.logger import get_logger

logger = get_logger(__name__)

# Maximum number of chunks per Qdrant upsert call (SPEC-03 guidance: 100).
UPSERT_BATCH_SIZE: int = 100


# ---------------------------------------------------------------------------
# Internal: deterministic Qdrant point ID
# ---------------------------------------------------------------------------

def _chunk_id_to_point_id(chunk_id: str) -> str:
    """Convert a 64-char SHA-256 hex chunk_id to a deterministic UUID string.

    Qdrant requires point IDs to be uint64 or UUID strings. We take the first
    128 bits (32 hex chars) of the SHA-256 digest and format as UUID.
    The collision probability for any realistic document corpus is negligible.

    The full chunk_id is always preserved as a payload field so downstream
    consumers can cross-reference without UUID arithmetic.

    Args:
        chunk_id: 64-character lowercase SHA-256 hex string.

    Returns:
        UUID string in canonical "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" format.
    """
    return str(uuid.UUID(chunk_id[:32]))


# ---------------------------------------------------------------------------
# VectorIndexer
# ---------------------------------------------------------------------------

class VectorIndexer:
    """Embeds chunks with sentence-transformers and upserts them to Qdrant.

    Evidence-only: this service is the sole writer to the Qdrant vector store.
    Writes are one-directional (ingest → Qdrant) and carry no graph authority.

    The service is safe to run concurrently across coroutines. The lazily-loaded
    embedding model is shared across batches within one instance.

    Args:
        settings:      Application settings (injectable for testing).
        qdrant_client: Pre-built AsyncQdrantClient (injectable for testing).
    """

    def __init__(
        self,
        settings: Any = None,
        qdrant_client: Any = None,
    ) -> None:
        from api.config import get_settings

        self._settings = settings or get_settings()
        self._qdrant: Any = qdrant_client  # None → created lazily on first use
        self._embedding_model: Any = None  # loaded lazily on first embed call

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def index_chunks(self, run_id: str, chunks: list[Chunk]) -> None:
        """Embed and upsert chunks to Qdrant.

        Processes chunks in batches of UPSERT_BATCH_SIZE. Within each batch,
        embedding is attempted first; if it fails, the batch is skipped and the
        next batch proceeds. Upsert failures follow the same pattern. Neither
        type of failure raises — ingestion must not halt because of vector
        indexing (evidence-only, CLAUDE.md §4.1).

        Collection creation is attempted once before the batch loop. If
        collection setup fails, the entire indexing call is abandoned (no
        collection → no upserts are possible) but an ERROR is logged rather
        than an exception being raised to the caller.

        Args:
            run_id: Governed run identifier (part of collection name and logs).
            chunks: Ordered list of Chunk instances to embed and index.

        Log events (SKILL-D R-D3):
            indexing_started    INFO  — run_id, collection, chunk_count
            collection_created  INFO  — collection, vector_dim, distance
            collection_exists   DEBUG — collection
            batch_indexed       INFO  — run_id, collection, batch_num,
                                        batch_size, total_indexed
            chunk_indexed       DEBUG — run_id, chunk_id, doc_id, collection
            indexing_failed     ERROR — run_id, collection, batch_num, error,
                                        stage ("embedding" | "upsert" |
                                               "collection_setup")
            indexing_complete   INFO  — run_id, collection, total_indexed,
                                        total_chunks
            index_chunks_skipped DEBUG — run_id, reason
        """
        if not chunks:
            logger.debug("index_chunks_skipped", run_id=run_id, reason="empty_list")
            return

        collection = f"chunks_{run_id}"

        logger.info(
            "indexing_started",
            run_id=run_id,
            collection=collection,
            chunk_count=len(chunks),
        )

        # Ensure the collection exists before any upsert attempt.
        # If this step fails we log ERROR and return — all batches would fail
        # without it, so there is no value in continuing.
        try:
            await self._ensure_collection(collection, run_id=run_id)
        except Exception as exc:
            logger.error(
                "indexing_failed",
                run_id=run_id,
                collection=collection,
                batch_num=0,
                error=str(exc),
                stage="collection_setup",
            )
            return

        total_indexed = 0
        total_batches = (len(chunks) + UPSERT_BATCH_SIZE - 1) // UPSERT_BATCH_SIZE

        for batch_num in range(total_batches):
            batch_start = batch_num * UPSERT_BATCH_SIZE
            batch_chunks = chunks[batch_start : batch_start + UPSERT_BATCH_SIZE]
            texts = [c.text for c in batch_chunks]  # not logged — SKILL-D R-D5

            # --- Embed this batch ---
            try:
                vectors = await asyncio.to_thread(self._embed_texts, texts)
            except Exception as exc:
                logger.error(
                    "indexing_failed",
                    run_id=run_id,
                    collection=collection,
                    batch_num=batch_num,
                    batch_start=batch_start,
                    batch_end=batch_start + len(batch_chunks) - 1,
                    error=str(exc),
                    stage="embedding",
                )
                continue  # skip batch; attempt remaining batches

            # --- Build PointStructs ---
            points = [
                self._build_point(chunk, vector)
                for chunk, vector in zip(batch_chunks, vectors)
            ]

            # --- Upsert to Qdrant ---
            try:
                client = self._get_qdrant_client()
                await client.upsert(collection_name=collection, points=points)

                total_indexed += len(points)

                logger.info(
                    "batch_indexed",
                    run_id=run_id,
                    collection=collection,
                    batch_num=batch_num,
                    batch_size=len(points),
                    total_indexed=total_indexed,
                )

                for chunk in batch_chunks:
                    logger.debug(
                        "chunk_indexed",
                        run_id=run_id,
                        chunk_id=chunk.chunk_id,
                        doc_id=chunk.doc_id,
                        collection=collection,
                    )

            except Exception as exc:
                logger.error(
                    "indexing_failed",
                    run_id=run_id,
                    collection=collection,
                    batch_num=batch_num,
                    batch_start=batch_start,
                    batch_end=batch_start + len(batch_chunks) - 1,
                    error=str(exc),
                    stage="upsert",
                )
                # Continue with remaining batches — partial indexing is acceptable
                # because Qdrant is evidence-only (CLAUDE.md §4.1).

        logger.info(
            "indexing_complete",
            run_id=run_id,
            collection=collection,
            total_indexed=total_indexed,
            total_chunks=len(chunks),
        )

    # ------------------------------------------------------------------
    # Public: chunk retrieval (evidence-only read path)
    # ------------------------------------------------------------------

    async def get_chunks_for_doc(
        self,
        run_id: str,
        doc_id: str,
    ) -> list[dict]:
        """Retrieve chunk payloads for a document from Qdrant via scroll.

        Uses a payload filter on doc_id and run_id so only chunks belonging
        to the requested document are returned.  All pages are scrolled until
        the result set is exhausted.

        Evidence-only: Qdrant is never authoritative.  Any Qdrant error is
        logged at ERROR level and an empty list is returned — the caller must
        not raise on a vector store failure (CLAUDE.md §4.1).

        Chunk text is present in the payload but is NOT included in the
        returned dicts — callers receive char_count (text length) instead so
        raw content is never surfaced through the listing endpoint
        (SKILL-D R-D5).

        Args:
            run_id: Governed run identifier (part of collection name).
            doc_id: 64-char SHA-256 document identifier.

        Returns:
            List of dicts with keys:
                chunk_id, start_page, end_page, start_page_locator,
                quality_flags (list[str]), text_length (int).
            Sorted by start_page_locator for stable presentation.
            Empty list on Qdrant error or absent collection.

        Log events:
            chunks_retrieved_for_doc  INFO  — run_id, doc_id, count
            chunk_retrieval_failed    ERROR — run_id, doc_id, error
        """
        collection = f"chunks_{run_id}"

        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue  # type: ignore[import-untyped]

            client = self._get_qdrant_client()
            doc_filter = Filter(
                must=[
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    FieldCondition(key="run_id", match=MatchValue(value=run_id)),
                ]
            )

            results = []
            offset = None
            while True:
                batch, next_offset = await client.scroll(
                    collection_name=collection,
                    scroll_filter=doc_filter,
                    limit=100,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                results.extend(batch)
                if next_offset is None:
                    break
                offset = next_offset

        except Exception as exc:
            logger.error(
                "chunk_retrieval_failed",
                run_id=run_id,
                doc_id=doc_id,
                error=str(exc),
            )
            return []

        payloads: list[dict] = []
        for point in results:
            payload = point.payload or {}
            payloads.append(
                {
                    "chunk_id": payload.get("chunk_id", ""),
                    "start_page": payload.get("start_page", 0),
                    "end_page": payload.get("end_page", 0),
                    "start_page_locator": payload.get("start_page_locator", ""),
                    "quality_flags": payload.get("quality_flags", []),
                    # text excluded — return length only (SKILL-D R-D5)
                    "text_length": len(payload.get("text", "")),
                }
            )

        # Sort by start_page_locator ("p{n}:c{i}") for stable ordering.
        payloads.sort(key=lambda p: p["start_page_locator"])

        logger.info(
            "chunks_retrieved_for_doc",
            run_id=run_id,
            doc_id=doc_id,
            count=len(payloads),
        )
        return payloads

    # ------------------------------------------------------------------
    # Internal: Qdrant client
    # ------------------------------------------------------------------

    def _get_qdrant_client(self) -> Any:
        """Return the AsyncQdrantClient, creating it on first call.

        Uses QDRANT_URL from settings when set; falls back to localhost:6333
        (docker-compose default) when None.
        """
        if self._qdrant is not None:
            return self._qdrant

        from qdrant_client import AsyncQdrantClient  # type: ignore[import-untyped]

        qdrant_url = self._settings.QDRANT_URL
        if qdrant_url:
            self._qdrant = AsyncQdrantClient(url=qdrant_url)
        else:
            self._qdrant = AsyncQdrantClient(host="localhost", port=6333)

        return self._qdrant

    # ------------------------------------------------------------------
    # Internal: collection management
    # ------------------------------------------------------------------

    async def _ensure_collection(self, collection: str, *, run_id: str) -> None:
        """Create the Qdrant collection if it does not already exist.

        Checks for existence via get_collection(); any exception is treated as
        "collection not found" and triggers creation. Vector dimensionality is
        determined by encoding a single representative text with the configured
        embedding model so that model changes are automatically reflected.

        Args:
            collection: Qdrant collection name (e.g. "chunks_{run_id}").
            run_id:     Governed run identifier (for log correlation only).

        Raises:
            Exception: Propagated from qdrant-client if create_collection fails.
        """
        from qdrant_client.models import Distance, VectorParams  # type: ignore[import-untyped]

        client = self._get_qdrant_client()

        try:
            await client.get_collection(collection_name=collection)
            logger.debug("collection_exists", collection=collection, run_id=run_id)
            return  # already exists — nothing to do
        except Exception:
            pass  # collection absent or unreachable — attempt creation below

        # Determine vector dimension from the embedding model.
        # Encode a single dummy text to read the output shape.
        sample: list[list[float]] = await asyncio.to_thread(
            self._embed_texts, ["dimension probe"]
        )
        vector_dim = len(sample[0])

        await client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
        )

        logger.info(
            "collection_created",
            collection=collection,
            vector_dim=vector_dim,
            distance="cosine",
            run_id=run_id,
        )

    # ------------------------------------------------------------------
    # Internal: embedding
    # ------------------------------------------------------------------

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts using the configured sentence-transformers model.

        Synchronous — always called via asyncio.to_thread.

        The model is loaded lazily on first call and reused for the lifetime of
        this VectorIndexer instance. Concurrent calls from separate to_thread
        invocations are safe: Python's GIL protects the simple attribute
        assignment during lazy load.

        Args:
            texts: List of strings to embed. Raw chunk text; never logged
                   (SKILL-D R-D5). Must be non-empty.

        Returns:
            List of float vectors in the same order as the input texts.

        Raises:
            ImportError: If sentence-transformers is not installed.
            Exception:   Any model-level encoding error.
        """
        if self._embedding_model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]

            model_name = self._settings.EMBEDDING_MODEL
            logger.info("embedding_model_loaded", model=model_name)
            self._embedding_model = SentenceTransformer(model_name)

        embeddings = self._embedding_model.encode(
            texts,
            batch_size=64,       # sentence-transformers internal batch size
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [emb.tolist() for emb in embeddings]

    # ------------------------------------------------------------------
    # Internal: point construction
    # ------------------------------------------------------------------

    def _build_point(self, chunk: Chunk, vector: list[float]) -> Any:
        """Build a Qdrant PointStruct from a Chunk and its embedding vector.

        Payload mirrors SPEC-03 S-03.5 metadata requirements exactly:
            chunk_id, doc_id, run_id, start_page, end_page,
            start_page_locator, quality_flags (as list), text.

        Point ID is the deterministic UUID from _chunk_id_to_point_id(). The
        full chunk_id is redundantly stored in payload for cross-referencing.

        Args:
            chunk:  Chunk instance (frozen Pydantic model).
            vector: Embedding vector for chunk.text.

        Returns:
            PointStruct ready for upsert.
        """
        from qdrant_client.models import PointStruct  # type: ignore[import-untyped]

        return PointStruct(
            id=_chunk_id_to_point_id(chunk.chunk_id),
            vector=vector,
            payload={
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "run_id": chunk.run_id,
                "start_page": chunk.start_page,
                "end_page": chunk.end_page,
                "start_page_locator": chunk.start_page_locator,
                # StrEnum values serialise to their string representations.
                "quality_flags": [str(f) for f in chunk.quality_flags],
                # text stored for evidence display — not logged (SKILL-D R-D5)
                "text": chunk.text,
            },
        )


# ---------------------------------------------------------------------------
# Process-level singleton factory
# ---------------------------------------------------------------------------

_service_instance: VectorIndexer | None = None


def get_vector_indexer() -> VectorIndexer:
    """Return the process-level VectorIndexer singleton.

    Created on first call (lazy). Settings are resolved at that point so
    startup env-var validation has already run.
    """
    global _service_instance
    if _service_instance is None:
        _service_instance = VectorIndexer()
    return _service_instance
