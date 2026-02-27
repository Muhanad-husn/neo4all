"""
api/vector/retriever.py — Evidence retrieval from Qdrant (SPEC-06 S-06.6).

Provides ranked chunk evidence for human curation decisions.  Evidence helps
operators understand why a candidate was flagged and what source text supports
a proposed mutation.

Evidence-only: this module is strictly read-only.  Qdrant is never authoritative
— Neo4j is the source of truth (CLAUDE.md §4.1).  All Qdrant errors degrade
gracefully: logged at ERROR, empty results returned, never raised to callers.

Three retrieval modes:

  by_dedupe_key(run_id, dedupe_key, top_k):
      Semantic search seeded from a graph element's primary value.  The
      primary_value is parsed from the node dedupe_key
      ("NodeType|primary_value|schema_version") and embedded as the query
      vector.  Use this to find chunks that mention a specific entity.

  by_doc(run_id, doc_id, limit, offset):
      Scroll all chunks belonging to a specific source document using a
      payload filter on doc_id.  Returns chunks in payload order (no vector
      ranking).  relevance_score is 0.0 for all results.

  by_query(run_id, query_text, top_k):
      Ad-hoc semantic search with a free-text query.  Embeds the query text
      and returns the top_k most similar chunks by cosine similarity.

Qdrant collection layout (per api/vector/indexer.py):
  Name:   chunks_{run_id}
  Payload fields per point:
    chunk_id           str   — 64-char SHA-256
    doc_id             str   — parent document identifier
    run_id             str   — governed run identifier
    start_page         int   — 0-indexed page of chunk start
    end_page           int   — 0-indexed page of chunk end
    start_page_locator str   — canonical locator "p{n}:c{i}"
    quality_flags      list  — ChunkQualityFlag string values
    text               str   — chunk text for evidence display

Sensitive data (SKILL-D R-D5):
  Chunk text is present in Qdrant payload and returned in EvidenceChunk.
  It must NOT be logged — only chunk_id is safe to log.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from api.observability.logger import get_logger

logger = get_logger(__name__)

# Maximum chunks returned per semantic search call.
MAX_SEMANTIC_RESULTS: int = 50

# Default page size for document scroll.
DEFAULT_DOC_LIMIT: int = 50


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class EvidenceChunk(BaseModel):
    """A single chunk of evidence retrieved from Qdrant.

    Attributes:
        chunk_id:           64-char SHA-256 digest (primary cross-reference).
        doc_id:             Source document identifier.
        run_id:             Governed run the chunk belongs to.
        text:               Full chunk text for display in the curation UI.
        start_page_locator: Canonical locator string "p{n}:c{i}".
        start_page:         0-indexed page number of the chunk start.
        end_page:           0-indexed page number of the chunk end.
        quality_flags:      ChunkQualityFlag values stamped during ingestion.
        relevance_score:    Cosine similarity [0.0, 1.0].  0.0 for non-vector
                            queries (by_doc) where ranking is not applicable.
    """

    chunk_id: str
    doc_id: str
    run_id: str
    text: str
    start_page_locator: str
    start_page: int
    end_page: int
    quality_flags: list[str]
    relevance_score: float


# ---------------------------------------------------------------------------
# EvidenceRetriever
# ---------------------------------------------------------------------------


class EvidenceRetriever:
    """Read-only evidence retrieval from the Qdrant vector store.

    Queries Qdrant by semantic similarity, source document, or a dedupe key's
    primary value.  All operations are read-only.  Qdrant errors degrade
    gracefully — empty lists are returned and ERROR is logged.

    The embedding model is loaded lazily on first use and reused for the
    lifetime of this instance (same pattern as VectorIndexer).

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
        self._qdrant: Any = qdrant_client  # None → created lazily
        self._embedding_model: Any = None  # loaded lazily on first embed

    # ------------------------------------------------------------------
    # Public retrieval methods
    # ------------------------------------------------------------------

    async def by_dedupe_key(
        self,
        run_id: str,
        dedupe_key: str,
        top_k: int = 10,
    ) -> list[EvidenceChunk]:
        """Return the top_k chunks most similar to a graph element's primary value.

        Parses the primary_value from the dedupe_key (format for nodes:
        "NodeType|primary_value|schema_version").  Falls back to the full
        dedupe_key string as the query if parsing fails.

        For relationship dedupe_keys ("RelType::start_key::end_key::sv"), the
        full string is used as the query — a deliberate approximation that
        errs toward broader recall.

        Args:
            run_id:     Governed run identifier (selects the Qdrant collection).
            dedupe_key: Deterministic identity key for a graph node or edge.
            top_k:      Maximum results to return (clamped to MAX_SEMANTIC_RESULTS).

        Returns:
            List of EvidenceChunk ordered by descending relevance_score.
            Empty list on Qdrant error or when the collection is absent.

        Log events:
            evidence_by_dedupe_key_start  DEBUG — run_id, dedupe_key, top_k
            evidence_retrieved            DEBUG — run_id, query_source, count
            evidence_retrieval_error      ERROR — run_id, mode, error
        """
        primary_value = _parse_primary_value(dedupe_key)
        logger.debug(
            "evidence_by_dedupe_key_start",
            run_id=run_id,
            dedupe_key=dedupe_key,
            top_k=top_k,
        )
        return await self.by_query(run_id, primary_value, top_k=top_k)

    async def by_doc(
        self,
        run_id: str,
        doc_id: str,
        limit: int = DEFAULT_DOC_LIMIT,
        offset: int = 0,
    ) -> list[EvidenceChunk]:
        """Return chunks from a specific source document via payload filter scroll.

        Uses Qdrant scroll (not search) with a filter on doc_id and run_id.
        Results are not ranked — relevance_score is 0.0 for all entries.

        Args:
            run_id:  Governed run identifier.
            doc_id:  Source document identifier.
            limit:   Maximum chunks to return per call.
            offset:  Number of results to skip (for manual pagination).

        Returns:
            List of EvidenceChunk with relevance_score=0.0.
            Empty list on Qdrant error or absent collection.

        Log events:
            evidence_by_doc_start   DEBUG — run_id, doc_id, limit, offset
            evidence_retrieved      DEBUG — run_id, query_source, count
            evidence_retrieval_error ERROR — run_id, mode, error
        """
        logger.debug(
            "evidence_by_doc_start",
            run_id=run_id,
            doc_id=doc_id,
            limit=limit,
            offset=offset,
        )
        collection = f"chunks_{run_id}"

        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue  # type: ignore[import-untyped]

            scroll_filter = Filter(
                must=[
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                    FieldCondition(key="run_id", match=MatchValue(value=run_id)),
                ]
            )
            client = self._get_qdrant_client()
            results, _next_offset = await client.scroll(
                collection_name=collection,
                scroll_filter=scroll_filter,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            logger.error(
                "evidence_retrieval_error",
                run_id=run_id,
                mode="by_doc",
                doc_id=doc_id,
                error=str(exc),
            )
            return []

        chunks = [
            self._from_payload(point.payload or {}, score=0.0) for point in results
        ]
        chunks = [c for c in chunks if c is not None]

        logger.debug(
            "evidence_retrieved",
            run_id=run_id,
            query_source="doc_id",
            count=len(chunks),
        )
        return chunks  # type: ignore[return-value]

    async def by_query(
        self,
        run_id: str,
        query_text: str,
        top_k: int = 10,
    ) -> list[EvidenceChunk]:
        """Return the top_k chunks most semantically similar to query_text.

        Embeds query_text using the configured sentence-transformers model and
        searches the run's Qdrant collection.  Results are filtered to the
        current run via a payload filter on run_id and ranked by cosine similarity.

        Args:
            run_id:     Governed run identifier.
            query_text: Free-text search query (not logged — SKILL-D R-D5).
            top_k:      Maximum results (clamped to MAX_SEMANTIC_RESULTS).

        Returns:
            List of EvidenceChunk ordered by descending relevance_score.
            Empty list on embedding failure, Qdrant error, or absent collection.

        Log events:
            evidence_by_query_start  DEBUG — run_id, top_k (no query_text)
            evidence_retrieved       DEBUG — run_id, query_source, count
            evidence_retrieval_error ERROR — run_id, mode, stage, error
        """
        top_k = min(top_k, MAX_SEMANTIC_RESULTS)
        collection = f"chunks_{run_id}"

        logger.debug("evidence_by_query_start", run_id=run_id, top_k=top_k)

        # Embed the query text (CPU-bound — run in thread).
        try:
            vectors: list[list[float]] = await asyncio.to_thread(
                self._embed_texts, [query_text]
            )
            query_vector = vectors[0]
        except Exception as exc:
            logger.error(
                "evidence_retrieval_error",
                run_id=run_id,
                mode="by_query",
                stage="embedding",
                error=str(exc),
            )
            return []

        # Search Qdrant — filter to this run to prevent cross-run leakage.
        # Uses query_points() (qdrant-client >= 1.12 removed the deprecated search()).
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue  # type: ignore[import-untyped]

            run_filter = Filter(
                must=[FieldCondition(key="run_id", match=MatchValue(value=run_id))]
            )
            client = self._get_qdrant_client()
            result = await client.query_points(
                collection_name=collection,
                query=query_vector,
                query_filter=run_filter,
                limit=top_k,
                with_payload=True,
                with_vectors=False,
            )
            hits = result.points
        except Exception as exc:
            logger.error(
                "evidence_retrieval_error",
                run_id=run_id,
                mode="by_query",
                stage="search",
                error=str(exc),
            )
            return []

        chunks = [
            self._from_payload(hit.payload or {}, score=float(hit.score))
            for hit in hits
        ]
        chunks = [c for c in chunks if c is not None]

        logger.debug(
            "evidence_retrieved",
            run_id=run_id,
            query_source="semantic",
            count=len(chunks),
        )
        return chunks  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Public: collection diagnostics
    # ------------------------------------------------------------------

    async def check_collection(self, run_id: str) -> dict[str, Any]:
        """Check if the Qdrant collection for a run exists and return point count.

        Returns a diagnostic dict:
            {"exists": bool, "point_count": int, "error": str | None}

        Never raises — returns error detail in the dict.
        """
        collection = f"chunks_{run_id}"
        try:
            client = self._get_qdrant_client()
            info = await client.get_collection(collection_name=collection)
            return {
                "exists": True,
                "point_count": info.points_count or 0,
                "error": None,
            }
        except Exception as exc:
            error_str = str(exc)
            # Qdrant returns specific errors for missing collections
            if "not found" in error_str.lower() or "doesn't exist" in error_str.lower():
                return {"exists": False, "point_count": 0, "error": None}
            return {"exists": False, "point_count": 0, "error": error_str}

    # ------------------------------------------------------------------
    # Internal: Qdrant client
    # ------------------------------------------------------------------

    def _get_qdrant_client(self) -> Any:
        """Return the AsyncQdrantClient, creating it lazily on first call."""
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
    # Internal: embedding
    # ------------------------------------------------------------------

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts using the configured sentence-transformers model.

        Synchronous — always called via asyncio.to_thread.  The model is
        loaded lazily on first call and reused for the instance lifetime.

        Args:
            texts: Texts to embed.  Never logged (SKILL-D R-D5).

        Returns:
            List of float vectors in the same order as the input.

        Raises:
            ImportError: sentence-transformers not installed.
            Exception:   Any encoding-level error.
        """
        if self._embedding_model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]

            model_name = self._settings.EMBEDDING_MODEL
            logger.info("embedding_model_loaded", model=model_name)
            self._embedding_model = SentenceTransformer(model_name)

        embeddings = self._embedding_model.encode(
            texts,
            batch_size=64,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [emb.tolist() for emb in embeddings]

    # ------------------------------------------------------------------
    # Internal: payload → EvidenceChunk
    # ------------------------------------------------------------------

    def _from_payload(
        self, payload: dict[str, Any], *, score: float
    ) -> EvidenceChunk | None:
        """Construct an EvidenceChunk from a Qdrant point payload.

        Returns None and logs a WARNING if required fields are missing — Qdrant
        may have incomplete payloads from an interrupted ingestion run.

        Args:
            payload: Raw Qdrant point payload dict.
            score:   Relevance score from the search hit (0.0 for scroll).

        Returns:
            EvidenceChunk on success, None if payload is missing required fields.

        Log events:
            evidence_incomplete_payload WARNING — chunk_id (if present), missing_fields
        """
        required = {"chunk_id", "doc_id", "run_id", "text", "start_page_locator"}
        missing = required - set(payload.keys())
        if missing:
            logger.warning(
                "evidence_incomplete_payload",
                chunk_id=payload.get("chunk_id", "<unknown>"),
                missing_fields=sorted(missing),
            )
            return None

        return EvidenceChunk(
            chunk_id=str(payload["chunk_id"]),
            doc_id=str(payload["doc_id"]),
            run_id=str(payload["run_id"]),
            text=str(payload["text"]),
            start_page_locator=str(payload["start_page_locator"]),
            start_page=int(payload.get("start_page", 0)),
            end_page=int(payload.get("end_page", 0)),
            quality_flags=list(payload.get("quality_flags", [])),
            relevance_score=score,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_primary_value(dedupe_key: str) -> str:
    """Extract the primary_value component from a node dedupe_key.

    Node dedupe_key format: "NodeType|primary_value|schema_version"
    Relationship dedupe_key format: "RelType::start::end::sv" (no "|")

    Returns primary_value for node keys, the full dedupe_key for rel keys
    or any key that does not match the expected node format.
    """
    parts = dedupe_key.split("|")
    if len(parts) >= 2:
        return parts[1]
    return dedupe_key


# ---------------------------------------------------------------------------
# Process-level singleton factory
# ---------------------------------------------------------------------------

_retriever_instance: EvidenceRetriever | None = None


def get_evidence_retriever() -> EvidenceRetriever:
    """Return the process-level EvidenceRetriever singleton.

    Created on first call (lazy).  Reuses the same Qdrant client and
    embedding model across requests — consistent with VectorIndexer's
    get_vector_indexer() pattern.
    """
    global _retriever_instance
    if _retriever_instance is None:
        _retriever_instance = EvidenceRetriever()
    return _retriever_instance
