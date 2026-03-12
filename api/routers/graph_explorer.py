"""
api/routers/graph_explorer.py — Paginated graph browse endpoints (SPEC-06 S-06.7).

Read-only, paginated views over the Neo4j graph for a governed run.  All reads
are delegated to GraphReader (cache-first, 5-min TTL per SKILL-D R-D8) — no
Cypher lives in this file.

Endpoints (mounted at /api/graph):

  GET /nodes/{run_id}
      Paginated node list with optional type filter.
      Max 5000 rows per page.  Page is 1-indexed.

  GET /nodes/{run_id}/count
      Total node count for a run, broken down by node_type.

  GET /edges/{run_id}
      Paginated edge list with optional type filter.
      Max 5000 rows per page.  Page is 1-indexed.

  GET /edges/{run_id}/count
      Total edge count for a run, broken down by rel_type.

Architecture (SKILL-B: thin router)
--------------------------------------
All graph reads are delegated to api/graph/reader.py.  Type filtering and
pagination are Python slice operations on the cached result — no additional
Cypher is needed.  GraphReader.get_nodes_by_type() is used when a node_type
filter is supplied (leverages its own cache entry separate from the full list).

Error handling
--------------
All endpoints:
  503 — Neo4j unavailable; graph read failed.
  200 — success (may be empty on first use before any extraction).

Caching (SKILL-D R-D8)
-----------------------
Delegated entirely to GraphReader.  This router performs no direct Redis calls.
The GraphReader's 5-minute TTL applies to all read results.
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, Query, Response

from api.graph.reader import GraphReader, GraphReaderInjectionError, get_graph_reader
from api.models.responses import ErrorDetail
from api.observability.logger import get_logger
from api.routers.graph_explorer_models import (
    EdgeCountResponse,
    EdgeOut,
    EdgePageResponse,
    NodeCountResponse,
    NodeOut,
    NodePageResponse,
    TypeCount,
    _DEFAULT_PAGE_SIZE,
    _MAX_PAGE_SIZE,
    _clamp_page,
    _clamp_page_size,
)

logger = get_logger(__name__)

router = APIRouter(tags=["graph-explorer"])


# ---------------------------------------------------------------------------
# GET /nodes/{run_id}/count
# (Registered BEFORE /nodes/{run_id} to prevent FastAPI from treating
#  "count" as the value of the run_id parameter on that route.)
# ---------------------------------------------------------------------------


@router.get(
    "/nodes/{run_id}/count",
    response_model=NodeCountResponse,
    summary="Total node count for a run, grouped by node_type",
    responses={
        503: {
            "model": NodeCountResponse,
            "description": "Neo4j unavailable",
        },
    },
)
async def count_nodes(
    run_id: str,
    response: Response,
    reader: GraphReader = Depends(get_graph_reader),
) -> NodeCountResponse:
    """Return the total number of nodes in a run, broken down by node_type.

    Uses the GraphReader's cached ``get_nodes_by_run`` result; no extra Neo4j
    query is issued if the result is already cached.

    **Errors**
    - ``503 Service Unavailable`` — Neo4j read failed.
    """
    logger.info("graph_explorer_node_count_requested", run_id=run_id)

    try:
        result = await reader.get_nodes_by_run(run_id)
    except Exception as exc:
        logger.error("graph_explorer_read_failed", run_id=run_id, error=str(exc))
        response.status_code = 503
        return NodeCountResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="neo4j_unavailable",
                    message=f"Graph read failed for run '{run_id}'.",
                )
            ],
        )

    counts: Counter[str] = Counter(n.node_type for n in result.nodes)
    by_type = sorted(
        [TypeCount(type=t, count=c) for t, c in counts.items()],
        key=lambda x: (-x.count, x.type),
    )

    logger.info(
        "graph_explorer_node_count_success",
        run_id=run_id,
        total=len(result.nodes),
        type_count=len(by_type),
    )

    return NodeCountResponse(
        run_id=run_id,
        status="success",
        total=len(result.nodes),
        by_type=by_type,
    )


# ---------------------------------------------------------------------------
# GET /nodes/{run_id}
# ---------------------------------------------------------------------------


@router.get(
    "/nodes/{run_id}",
    response_model=NodePageResponse,
    summary="Paginated node list with optional node_type filter (max 5000/page)",
    responses={
        400: {
            "model": NodePageResponse,
            "description": "node_type contains invalid identifier characters",
        },
        503: {
            "model": NodePageResponse,
            "description": "Neo4j unavailable",
        },
    },
)
async def list_nodes(
    run_id: str,
    response: Response,
    page: int = Query(1, ge=1, description="1-indexed page number"),
    page_size: int = Query(
        _DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE, description="Rows per page (max 5000)"
    ),
    node_type: str | None = Query(None, description="Filter by node label (exact match)"),
    reader: GraphReader = Depends(get_graph_reader),
) -> NodePageResponse:
    """Return a paginated list of nodes for a run.

    Nodes are sorted by dedupe_key for deterministic ordering.  Filtering by
    ``node_type`` uses GraphReader's typed query (cached separately from the
    full-run result).

    **Errors**
    - ``400 Bad Request`` — node_type contains characters invalid for a Neo4j
      label identifier.
    - ``503 Service Unavailable`` — Neo4j read failed.
    """
    page = _clamp_page(page)
    page_size = _clamp_page_size(page_size)

    logger.info(
        "graph_explorer_nodes_requested",
        run_id=run_id,
        page=page,
        page_size=page_size,
        node_type=node_type,
    )

    try:
        if node_type:
            result = await reader.get_nodes_by_type(run_id, node_type)
        else:
            result = await reader.get_nodes_by_run(run_id)
    except GraphReaderInjectionError as exc:
        response.status_code = 400
        return NodePageResponse(
            run_id=run_id,
            status="error",
            errors=[ErrorDetail(code="invalid_node_type", message=str(exc))],
        )
    except Exception as exc:
        logger.error("graph_explorer_read_failed", run_id=run_id, error=str(exc))
        response.status_code = 503
        return NodePageResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="neo4j_unavailable",
                    message=f"Graph read failed for run '{run_id}'.",
                )
            ],
        )

    total = len(result.nodes)
    start = (page - 1) * page_size
    end = start + page_size
    page_nodes = result.nodes[start:end]
    has_more = end < total

    items = [
        NodeOut(
            dedupe_key=n.dedupe_key,
            node_type=n.node_type,
            run_id=n.run_id,
            schema_version=n.schema_version,
            properties=dict(n.properties),
        )
        for n in page_nodes
    ]

    logger.info(
        "graph_explorer_nodes_success",
        run_id=run_id,
        page=page,
        page_size=page_size,
        total=total,
        returned=len(items),
    )

    return NodePageResponse(
        run_id=run_id,
        status="success",
        page=page,
        page_size=page_size,
        total=total,
        has_more=has_more,
        items=items,
    )


# ---------------------------------------------------------------------------
# GET /edges/{run_id}/count
# (Registered BEFORE /edges/{run_id} for the same reason as /nodes.)
# ---------------------------------------------------------------------------


@router.get(
    "/edges/{run_id}/count",
    response_model=EdgeCountResponse,
    summary="Total edge count for a run, grouped by rel_type",
    responses={
        503: {
            "model": EdgeCountResponse,
            "description": "Neo4j unavailable",
        },
    },
)
async def count_edges(
    run_id: str,
    response: Response,
    reader: GraphReader = Depends(get_graph_reader),
) -> EdgeCountResponse:
    """Return the total number of edges in a run, broken down by rel_type.

    **Errors**
    - ``503 Service Unavailable`` — Neo4j read failed.
    """
    logger.info("graph_explorer_edge_count_requested", run_id=run_id)

    try:
        result = await reader.get_relationships_by_run(run_id)
    except Exception as exc:
        logger.error("graph_explorer_read_failed", run_id=run_id, error=str(exc))
        response.status_code = 503
        return EdgeCountResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="neo4j_unavailable",
                    message=f"Graph read failed for run '{run_id}'.",
                )
            ],
        )

    counts: Counter[str] = Counter(r.rel_type for r in result.rels)
    by_type = sorted(
        [TypeCount(type=t, count=c) for t, c in counts.items()],
        key=lambda x: (-x.count, x.type),
    )

    logger.info(
        "graph_explorer_edge_count_success",
        run_id=run_id,
        total=len(result.rels),
        type_count=len(by_type),
    )

    return EdgeCountResponse(
        run_id=run_id,
        status="success",
        total=len(result.rels),
        by_type=by_type,
    )


# ---------------------------------------------------------------------------
# GET /edges/{run_id}
# ---------------------------------------------------------------------------


@router.get(
    "/edges/{run_id}",
    response_model=EdgePageResponse,
    summary="Paginated edge list with optional edge_type filter (max 5000/page)",
    responses={
        503: {
            "model": EdgePageResponse,
            "description": "Neo4j unavailable",
        },
    },
)
async def list_edges(
    run_id: str,
    response: Response,
    page: int = Query(1, ge=1, description="1-indexed page number"),
    page_size: int = Query(
        _DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE, description="Rows per page (max 5000)"
    ),
    edge_type: str | None = Query(None, description="Filter by relationship type (exact match)"),
    reader: GraphReader = Depends(get_graph_reader),
) -> EdgePageResponse:
    """Return a paginated list of directed edges for a run.

    Edges are sorted by dedupe_key for deterministic ordering.  When
    ``edge_type`` is supplied, the full edge list is filtered in Python (there
    is no separate cached query for edge-type subsets, unlike nodes).

    **Errors**
    - ``503 Service Unavailable`` — Neo4j read failed.
    """
    page = _clamp_page(page)
    page_size = _clamp_page_size(page_size)

    logger.info(
        "graph_explorer_edges_requested",
        run_id=run_id,
        page=page,
        page_size=page_size,
        edge_type=edge_type,
    )

    try:
        result = await reader.get_relationships_by_run(run_id)
    except Exception as exc:
        logger.error("graph_explorer_read_failed", run_id=run_id, error=str(exc))
        response.status_code = 503
        return EdgePageResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="neo4j_unavailable",
                    message=f"Graph read failed for run '{run_id}'.",
                )
            ],
        )

    rels = result.rels
    if edge_type:
        rels = tuple(r for r in rels if r.rel_type == edge_type)

    total = len(rels)
    start = (page - 1) * page_size
    end = start + page_size
    page_rels = rels[start:end]
    has_more = end < total

    items = [
        EdgeOut(
            dedupe_key=r.dedupe_key,
            rel_type=r.rel_type,
            start_dedupe_key=r.start_dedupe_key,
            end_dedupe_key=r.end_dedupe_key,
            run_id=r.run_id,
            schema_version=r.schema_version,
            properties=dict(r.properties),
        )
        for r in page_rels
    ]

    logger.info(
        "graph_explorer_edges_success",
        run_id=run_id,
        page=page,
        page_size=page_size,
        total=total,
        returned=len(items),
    )

    return EdgePageResponse(
        run_id=run_id,
        status="success",
        page=page,
        page_size=page_size,
        total=total,
        has_more=has_more,
        items=items,
    )
