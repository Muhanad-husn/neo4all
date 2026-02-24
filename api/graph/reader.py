"""
api/graph/reader.py — Read-only Neo4j queries for candidate detection (SPEC-05 S-05.3).

Parameterization: ALL data values flow as named Cypher parameters ($param).
Node labels are embedded only after _validate_identifier() validates them
against ^[A-Za-z][A-Za-z0-9_]*$ — failure raises GraphReaderInjectionError.

Caching: every method checks CacheKey.graph_query(run_id, query_hash) before
querying Neo4j (TTL 300 s).  Cache misses fall through silently (DEBUG).
Redis failures log at WARNING and never block reads (SKILL-D R-D9).

Result models: api/graph/reader_models.py (split per SKILL-B R-B7).
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from typing import Any

from api.cache.client import CacheClient, get_cache_client
from api.cache.keys import CacheKey
from api.graph.client import Neo4jClient, get_neo4j_client
from api.graph.reader_models import (
    DegreeListResult,
    DegreeRecord,
    GraphNodeRecord,
    GraphRelRecord,
    NeighborResult,
    NodeListResult,
    OrphanResult,
    RelListResult,
)
from api.observability.logger import get_logger

logger = get_logger(__name__)

# 5-minute TTL for graph reader query results (SKILL-D R-D8)
GRAPH_QUERY_TTL: int = 300

_SAFE_IDENTIFIER_RE: re.Pattern[str] = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


# ---------------------------------------------------------------------------
# Injection guard
# ---------------------------------------------------------------------------


class GraphReaderInjectionError(ValueError):
    """Raised when a node label fails identifier validation before embedding in Cypher."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_query_hash(method: str, params: dict[str, Any]) -> str:
    """Deterministic SHA-256 of method + params for CacheKey.graph_query()."""
    payload = json.dumps(
        {"method": method, "params": params},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_identifier(value: str, kind: str) -> str:
    """Return value if safe to embed as a Neo4j label, else raise GraphReaderInjectionError."""
    if not _SAFE_IDENTIFIER_RE.match(value):
        raise GraphReaderInjectionError(
            f"Invalid Neo4j identifier for {kind}: {value!r}. "
            f"Must match ^[A-Za-z][A-Za-z0-9_]*$."
        )
    return value


def _serialize_value(v: Any) -> Any:
    """Convert a Neo4j value to a JSON-serialisable primitive (bool before int)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, str)):
        return v
    if isinstance(v, list):
        return [_serialize_value(item) for item in v]
    return str(v)


def _to_node(row: dict[str, Any]) -> GraphNodeRecord:
    raw: dict[str, Any] = row.get("props") or {}
    return GraphNodeRecord(
        dedupe_key=str(row["dedupe_key"]),
        node_type=str(row["node_type"]),
        run_id=str(row["run_id"]),
        schema_version=str(row["schema_version"]),
        properties={k: _serialize_value(v) for k, v in raw.items()},
    )


def _to_rel(row: dict[str, Any]) -> GraphRelRecord:
    raw: dict[str, Any] = row.get("props") or {}
    return GraphRelRecord(
        dedupe_key=str(row["dedupe_key"]),
        rel_type=str(row["rel_type"]),
        start_dedupe_key=str(row["start_dedupe_key"]),
        end_dedupe_key=str(row["end_dedupe_key"]),
        run_id=str(row["run_id"]),
        schema_version=str(row["schema_version"]),
        properties={k: _serialize_value(v) for k, v in raw.items()},
    )


# ---------------------------------------------------------------------------
# GraphReader
# ---------------------------------------------------------------------------


class GraphReader:
    """Read-only Neo4j query executor with cache-first lookup.

    Obtain via get_graph_reader().  All methods check Redis before querying
    Neo4j.  The Neo4j driver must be open (FastAPI lifespan) before use.
    """

    def __init__(self, neo4j_client: Neo4jClient, cache: CacheClient) -> None:
        self._neo4j = neo4j_client
        self._cache = cache

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    async def _cache_get(self, key: str, model: type, method: str, run_id: str) -> Any:
        cached = await self._cache.get(key, model=model)  # type: ignore[type-var]
        if cached is None:
            logger.debug("graph_query_cache_miss", method=method, run_id=run_id)
        return cached

    async def _run_read(self, cypher: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Execute a read transaction and return rows as list-of-dicts."""
        async def _fetch(tx: Any) -> list[dict[str, Any]]:
            result = await tx.run(cypher, **params)
            return await result.data()

        async with self._neo4j.acquire_session() as session:
            return await session.execute_read(_fetch)

    # ------------------------------------------------------------------
    # Public read methods
    # ------------------------------------------------------------------

    async def get_nodes_by_run(self, run_id: str) -> NodeListResult:
        """Return all nodes belonging to a run (exact-dup + anomaly detectors)."""
        qhash = _compute_query_hash("get_nodes_by_run", {"run_id": run_id})
        key = CacheKey.graph_query(run_id=run_id, query_hash=qhash)

        cached = await self._cache_get(key, NodeListResult, "get_nodes_by_run", run_id)
        if cached is not None:
            return cached

        rows = await self._run_read(
            """
            MATCH (n)
            WHERE n._run_id = $run_id
            RETURN n._dedupe_key     AS dedupe_key,
                   labels(n)[0]      AS node_type,
                   n._run_id         AS run_id,
                   n._schema_version AS schema_version,
                   properties(n)     AS props
            ORDER BY n._dedupe_key
            """,
            {"run_id": run_id},
        )
        result = NodeListResult(nodes=tuple(_to_node(r) for r in rows))
        logger.debug(
            "graph_query_executed",
            method="get_nodes_by_run",
            run_id=run_id,
            count=len(result.nodes),
        )
        await self._cache.set(key, result, ttl=GRAPH_QUERY_TTL)
        return result

    async def get_nodes_by_type(self, run_id: str, node_type: str) -> NodeListResult:
        """Return all nodes of a specific label for a run.

        Raises GraphReaderInjectionError if node_type fails identifier validation.
        """
        _validate_identifier(node_type, "node_type")

        qhash = _compute_query_hash(
            "get_nodes_by_type", {"run_id": run_id, "node_type": node_type}
        )
        key = CacheKey.graph_query(run_id=run_id, query_hash=qhash)

        cached = await self._cache_get(key, NodeListResult, "get_nodes_by_type", run_id)
        if cached is not None:
            return cached

        # node_type backtick-quoted after regex validation (defence in depth).
        rows = await self._run_read(
            f"""
            MATCH (n:`{node_type}`)
            WHERE n._run_id = $run_id
            RETURN n._dedupe_key     AS dedupe_key,
                   $node_type        AS node_type,
                   n._run_id         AS run_id,
                   n._schema_version AS schema_version,
                   properties(n)     AS props
            ORDER BY n._dedupe_key
            """,
            {"run_id": run_id, "node_type": node_type},
        )
        result = NodeListResult(nodes=tuple(_to_node(r) for r in rows))
        logger.debug(
            "graph_query_executed",
            method="get_nodes_by_type",
            run_id=run_id,
            node_type=node_type,
            count=len(result.nodes),
        )
        await self._cache.set(key, result, ttl=GRAPH_QUERY_TTL)
        return result

    async def get_relationships_by_run(self, run_id: str) -> RelListResult:
        """Return all directed relationships for a run (exact-dup + canonical-violation)."""
        qhash = _compute_query_hash("get_relationships_by_run", {"run_id": run_id})
        key = CacheKey.graph_query(run_id=run_id, query_hash=qhash)

        cached = await self._cache_get(
            key, RelListResult, "get_relationships_by_run", run_id
        )
        if cached is not None:
            return cached

        rows = await self._run_read(
            """
            MATCH (start)-[r]->(end)
            WHERE r._run_id = $run_id
            RETURN r._dedupe_key      AS dedupe_key,
                   type(r)            AS rel_type,
                   start._dedupe_key  AS start_dedupe_key,
                   end._dedupe_key    AS end_dedupe_key,
                   r._run_id          AS run_id,
                   r._schema_version  AS schema_version,
                   properties(r)      AS props
            ORDER BY r._dedupe_key
            """,
            {"run_id": run_id},
        )
        result = RelListResult(rels=tuple(_to_rel(r) for r in rows))
        logger.debug(
            "graph_query_executed",
            method="get_relationships_by_run",
            run_id=run_id,
            count=len(result.rels),
        )
        await self._cache.set(key, result, ttl=GRAPH_QUERY_TTL)
        return result

    async def get_neighbors(self, run_id: str, dedupe_key: str) -> NeighborResult:
        """Return the undirected neighbor set of a node (probable-dup detector).

        DISTINCT eliminates duplicates from multi-edge connections.
        """
        qhash = _compute_query_hash(
            "get_neighbors", {"run_id": run_id, "dedupe_key": dedupe_key}
        )
        key = CacheKey.graph_query(run_id=run_id, query_hash=qhash)

        cached = await self._cache_get(key, NeighborResult, "get_neighbors", run_id)
        if cached is not None:
            return cached

        rows = await self._run_read(
            """
            MATCH (n)-[]-(neighbor)
            WHERE n._dedupe_key = $dedupe_key
              AND n._run_id     = $run_id
            RETURN DISTINCT
                   neighbor._dedupe_key     AS dedupe_key,
                   labels(neighbor)[0]      AS node_type,
                   neighbor._run_id         AS run_id,
                   neighbor._schema_version AS schema_version,
                   properties(neighbor)     AS props
            ORDER BY neighbor._dedupe_key
            """,
            {"dedupe_key": dedupe_key, "run_id": run_id},
        )
        result = NeighborResult(
            node_dedupe_key=dedupe_key,
            neighbors=tuple(_to_node(r) for r in rows),
        )
        logger.debug(
            "graph_query_executed",
            method="get_neighbors",
            run_id=run_id,
            dedupe_key=dedupe_key,
            count=len(result.neighbors),
        )
        await self._cache.set(key, result, ttl=GRAPH_QUERY_TTL)
        return result

    async def get_orphans(self, run_id: str) -> OrphanResult:
        """Return dedupe_keys of all degree-zero nodes in a run (anomaly detector)."""
        qhash = _compute_query_hash("get_orphans", {"run_id": run_id})
        key = CacheKey.graph_query(run_id=run_id, query_hash=qhash)

        cached = await self._cache_get(key, OrphanResult, "get_orphans", run_id)
        if cached is not None:
            return cached

        rows = await self._run_read(
            """
            MATCH (n)
            WHERE n._run_id = $run_id
              AND NOT (n)--()
            RETURN n._dedupe_key AS dedupe_key
            ORDER BY n._dedupe_key
            """,
            {"run_id": run_id},
        )
        result = OrphanResult(
            orphan_dedupe_keys=tuple(str(r["dedupe_key"]) for r in rows)
        )
        logger.debug(
            "graph_query_executed",
            method="get_orphans",
            run_id=run_id,
            count=len(result.orphan_dedupe_keys),
        )
        await self._cache.set(key, result, ttl=GRAPH_QUERY_TTL)
        return result

    async def get_node_degrees(self, run_id: str) -> DegreeListResult:
        """Return in/out degree for every node in a run (anomaly detector).

        OPTIONAL MATCH + count(DISTINCT) avoids inflated counts from multi-edge paths.
        """
        qhash = _compute_query_hash("get_node_degrees", {"run_id": run_id})
        key = CacheKey.graph_query(run_id=run_id, query_hash=qhash)

        cached = await self._cache_get(
            key, DegreeListResult, "get_node_degrees", run_id
        )
        if cached is not None:
            return cached

        rows = await self._run_read(
            """
            MATCH (n)
            WHERE n._run_id = $run_id
            OPTIONAL MATCH (n)<-[in_rel]-()
            OPTIONAL MATCH (n)-[out_rel]->()
            WITH n,
                 count(DISTINCT in_rel)  AS in_degree,
                 count(DISTINCT out_rel) AS out_degree
            RETURN n._dedupe_key AS dedupe_key,
                   in_degree,
                   out_degree
            ORDER BY n._dedupe_key
            """,
            {"run_id": run_id},
        )
        result = DegreeListResult(
            degrees=tuple(
                DegreeRecord(
                    dedupe_key=str(r["dedupe_key"]),
                    in_degree=int(r["in_degree"]),
                    out_degree=int(r["out_degree"]),
                )
                for r in rows
            )
        )
        logger.debug(
            "graph_query_executed",
            method="get_node_degrees",
            run_id=run_id,
            count=len(result.degrees),
        )
        await self._cache.set(key, result, ttl=GRAPH_QUERY_TTL)
        return result


# ---------------------------------------------------------------------------
# Process-level singleton
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_graph_reader() -> GraphReader:
    """Return the process-level GraphReader singleton.  Driver must be open."""
    return GraphReader(neo4j_client=get_neo4j_client(), cache=get_cache_client())
