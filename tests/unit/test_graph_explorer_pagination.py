"""
tests/unit/test_graph_explorer_pagination.py — Graph explorer pagination tests
(SPEC-06 file 20).

No network, no Neo4j.  GraphReader is replaced by in-process fakes injected
via FastAPI's dependency_overrides mechanism.

Coverage:
  Node pagination:
    - Page 1 of 130 nodes: 50 items returned, has_more=True
    - Page 2 of 130 nodes: 50 items returned, has_more=True
    - Page 3 of 130 nodes: 30 items returned, has_more=False
    - Page 4 (beyond last) of 130 nodes: 0 items, has_more=False
    - Page size clamped to _MAX_PAGE_SIZE (50) when a larger value is requested
    - Fewer nodes than page_size: all returned on page 1, has_more=False

  node_type filter:
    - Mixed 80-Person / 50-Organisation corpus → filter Person → 80 total, 50 on page 1
    - Filter returns empty when no nodes match the type
    - GraphReaderInjectionError from get_nodes_by_type → 400 Bad Request

  Edge pagination:
    - Page 1 of 75 edges: 50 items, has_more=True
    - Page 2 of 75 edges: 25 items, has_more=False
    - Page beyond end: 0 items, has_more=False

  edge_type filter:
    - Mixed 40-KNOWS / 35-WORKS corpus → filter KNOWS → 40 total

  Count endpoints:
    - Node count: correct total, correct by_type breakdown, descending sort
    - Edge count: correct total, correct by_type breakdown

  Error handling:
    - Neo4j read failure → 503 Service Unavailable (nodes, edges, count variants)
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.graph.reader import GraphReaderInjectionError, get_graph_reader
from api.graph.reader_models import (
    GraphNodeRecord,
    GraphRelRecord,
    NodeListResult,
    RelListResult,
)
from api.routers.graph_explorer import _MAX_PAGE_SIZE, router

# ---------------------------------------------------------------------------
# Module-level test application
# ---------------------------------------------------------------------------

_app = FastAPI()
_app.include_router(router, prefix="/graph")

_RUN_ID = "run-explorer-unit-test-01"


# ---------------------------------------------------------------------------
# Test data factories
# ---------------------------------------------------------------------------


def _node(i: int, node_type: str = "Person") -> GraphNodeRecord:
    return GraphNodeRecord(
        dedupe_key=f"{node_type}|node-{i:04d}|v1",
        node_type=node_type,
        run_id=_RUN_ID,
        schema_version="v1",
        properties={"index": i},
    )


def _edge(i: int, rel_type: str = "KNOWS") -> GraphRelRecord:
    return GraphRelRecord(
        dedupe_key=f"{rel_type}|edge-{i:04d}|v1",
        rel_type=rel_type,
        start_dedupe_key=f"Person|node-{i:04d}|v1",
        end_dedupe_key=f"Person|node-{i + 1:04d}|v1",
        run_id=_RUN_ID,
        schema_version="v1",
        properties={"weight": i},
    )


def _node_result(*args: GraphNodeRecord) -> NodeListResult:
    return NodeListResult(nodes=args)


def _rel_result(*args: GraphRelRecord) -> RelListResult:
    return RelListResult(rels=args)


# ---------------------------------------------------------------------------
# Fake GraphReader
# ---------------------------------------------------------------------------


class _FakeReader:
    """In-process GraphReader replacement for unit tests.

    Raises _type_error on get_nodes_by_type() when set; otherwise filters
    the _nodes result by node_type.  Raises _read_error on any method when
    set (simulates Neo4j unavailability).
    """

    def __init__(
        self,
        nodes: NodeListResult | None = None,
        rels: RelListResult | None = None,
        type_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self._nodes = nodes or NodeListResult(nodes=())
        self._rels = rels or RelListResult(rels=())
        self._type_error = type_error
        self._read_error = read_error

    async def get_nodes_by_run(self, run_id: str) -> NodeListResult:
        if self._read_error:
            raise self._read_error
        return self._nodes

    async def get_nodes_by_type(self, run_id: str, node_type: str) -> NodeListResult:
        if self._read_error:
            raise self._read_error
        if self._type_error:
            raise self._type_error
        filtered = tuple(n for n in self._nodes.nodes if n.node_type == node_type)
        return NodeListResult(nodes=filtered)

    async def get_relationships_by_run(self, run_id: str) -> RelListResult:
        if self._read_error:
            raise self._read_error
        return self._rels


def _client(reader: _FakeReader) -> TestClient:
    """Return a TestClient with the supplied fake reader injected."""
    _app.dependency_overrides[get_graph_reader] = lambda: reader
    return TestClient(_app, raise_server_exceptions=False)


def _clear() -> None:
    _app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Node pagination — page boundaries
# ---------------------------------------------------------------------------


def test_node_page_1_of_130() -> None:
    """Page 1 of 130 nodes returns 50 items, has_more=True."""
    nodes = NodeListResult(nodes=tuple(_node(i) for i in range(130)))
    reader = _FakeReader(nodes=nodes)
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["total"] == 130
    assert len(data["items"]) == 50
    assert data["has_more"] is True
    assert data["page"] == 1
    assert data["page_size"] == _MAX_PAGE_SIZE


def test_node_page_2_of_130() -> None:
    """Page 2 of 130 nodes returns 50 items, has_more=True."""
    nodes = NodeListResult(nodes=tuple(_node(i) for i in range(130)))
    reader = _FakeReader(nodes=nodes)
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}?page=2")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 130
    assert len(data["items"]) == 50
    assert data["has_more"] is True
    assert data["page"] == 2


def test_node_page_3_of_130_is_last() -> None:
    """Page 3 of 130 nodes returns 30 items (remainder), has_more=False."""
    nodes = NodeListResult(nodes=tuple(_node(i) for i in range(130)))
    reader = _FakeReader(nodes=nodes)
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}?page=3")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 130
    assert len(data["items"]) == 30
    assert data["has_more"] is False
    assert data["page"] == 3


def test_node_page_beyond_end_returns_empty() -> None:
    """Requesting a page past the last page returns 0 items, has_more=False."""
    nodes = NodeListResult(nodes=tuple(_node(i) for i in range(130)))
    reader = _FakeReader(nodes=nodes)
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}?page=4")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 130
    assert len(data["items"]) == 0
    assert data["has_more"] is False


def test_node_fewer_than_page_size_returns_all() -> None:
    """25 nodes all fit on page 1; has_more=False."""
    nodes = NodeListResult(nodes=tuple(_node(i) for i in range(25)))
    reader = _FakeReader(nodes=nodes)
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 25
    assert len(data["items"]) == 25
    assert data["has_more"] is False


# ---------------------------------------------------------------------------
# Page size clamping
# ---------------------------------------------------------------------------


def test_node_page_size_capped_at_max() -> None:
    """FastAPI validates page_size ≤ 50; requesting 100 is rejected with 422."""
    nodes = NodeListResult(nodes=tuple(_node(i) for i in range(200)))
    reader = _FakeReader(nodes=nodes)
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}?page_size=100")
    _clear()

    # FastAPI's Query(le=50) rejects values above the limit.
    assert resp.status_code == 422


def test_node_explicit_page_size_respected() -> None:
    """page_size=10 returns at most 10 items per page."""
    nodes = NodeListResult(nodes=tuple(_node(i) for i in range(35)))
    reader = _FakeReader(nodes=nodes)
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}?page_size=10&page=1")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 10
    assert data["total"] == 35
    assert data["has_more"] is True


# ---------------------------------------------------------------------------
# node_type filter
# ---------------------------------------------------------------------------


def test_node_type_filter_returns_only_matching() -> None:
    """node_type=Person filters the corpus to 80 Person nodes (50 on page 1)."""
    all_nodes = (
        tuple(_node(i, "Person") for i in range(80))
        + tuple(_node(i, "Organisation") for i in range(50))
    )
    nodes = NodeListResult(nodes=all_nodes)
    reader = _FakeReader(nodes=nodes)
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}?node_type=Person")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 80
    assert len(data["items"]) == 50
    assert data["has_more"] is True
    assert all(item["node_type"] == "Person" for item in data["items"])


def test_node_type_filter_no_match_returns_empty() -> None:
    """Filtering by a type that doesn't exist returns 0 items."""
    nodes = NodeListResult(nodes=tuple(_node(i, "Person") for i in range(30)))
    reader = _FakeReader(nodes=nodes)
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}?node_type=Organisation")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []
    assert data["has_more"] is False


def test_node_type_injection_returns_400() -> None:
    """GraphReaderInjectionError from get_nodes_by_type yields 400 Bad Request."""
    nodes = NodeListResult(nodes=tuple(_node(i) for i in range(10)))
    reader = _FakeReader(
        nodes=nodes,
        type_error=GraphReaderInjectionError("invalid label: 'DROP TABLE'"),
    )
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}?node_type=DROP+TABLE")
    _clear()

    assert resp.status_code == 400
    data = resp.json()
    assert data["status"] == "error"
    assert any(e["code"] == "invalid_node_type" for e in data["errors"])


# ---------------------------------------------------------------------------
# Edge pagination
# ---------------------------------------------------------------------------


def test_edge_page_1_of_75() -> None:
    """Page 1 of 75 edges returns 50 items, has_more=True."""
    rels = RelListResult(rels=tuple(_edge(i) for i in range(75)))
    reader = _FakeReader(rels=rels)
    with _client(reader) as c:
        resp = c.get(f"/graph/edges/{_RUN_ID}")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 75
    assert len(data["items"]) == 50
    assert data["has_more"] is True
    assert data["page"] == 1


def test_edge_page_2_of_75_is_last() -> None:
    """Page 2 of 75 edges returns 25 items, has_more=False."""
    rels = RelListResult(rels=tuple(_edge(i) for i in range(75)))
    reader = _FakeReader(rels=rels)
    with _client(reader) as c:
        resp = c.get(f"/graph/edges/{_RUN_ID}?page=2")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 75
    assert len(data["items"]) == 25
    assert data["has_more"] is False


def test_edge_page_beyond_end_returns_empty() -> None:
    """Requesting a page past the last edge page returns 0 items."""
    rels = RelListResult(rels=tuple(_edge(i) for i in range(75)))
    reader = _FakeReader(rels=rels)
    with _client(reader) as c:
        resp = c.get(f"/graph/edges/{_RUN_ID}?page=3")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 75
    assert len(data["items"]) == 0
    assert data["has_more"] is False


# ---------------------------------------------------------------------------
# edge_type filter
# ---------------------------------------------------------------------------


def test_edge_type_filter_returns_only_matching() -> None:
    """edge_type=KNOWS filters the corpus to 40 KNOWS edges (all on page 1)."""
    all_rels = (
        tuple(_edge(i, "KNOWS") for i in range(40))
        + tuple(_edge(i, "WORKS_FOR") for i in range(35))
    )
    rels = RelListResult(rels=all_rels)
    reader = _FakeReader(rels=rels)
    with _client(reader) as c:
        resp = c.get(f"/graph/edges/{_RUN_ID}?edge_type=KNOWS")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 40
    assert len(data["items"]) == 40
    assert data["has_more"] is False
    assert all(item["rel_type"] == "KNOWS" for item in data["items"])


def test_edge_type_filter_no_match_returns_empty() -> None:
    """edge_type filter that matches nothing returns 0 items."""
    rels = RelListResult(rels=tuple(_edge(i, "KNOWS") for i in range(20)))
    reader = _FakeReader(rels=rels)
    with _client(reader) as c:
        resp = c.get(f"/graph/edges/{_RUN_ID}?edge_type=MENTORS")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


# ---------------------------------------------------------------------------
# Count endpoints
# ---------------------------------------------------------------------------


def test_node_count_by_type() -> None:
    """GET /nodes/{run_id}/count returns correct total and by_type sorted desc."""
    all_nodes = (
        tuple(_node(i, "Person") for i in range(60))
        + tuple(_node(i, "Organisation") for i in range(25))
        + tuple(_node(i, "Location") for i in range(15))
    )
    nodes = NodeListResult(nodes=all_nodes)
    reader = _FakeReader(nodes=nodes)
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}/count")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["total"] == 100
    # by_type sorted descending by count
    types = [(item["type"], item["count"]) for item in data["by_type"]]
    assert types[0] == ("Person", 60)
    assert types[1] == ("Organisation", 25)
    assert types[2] == ("Location", 15)


def test_node_count_empty_run() -> None:
    """Count endpoint returns total=0 and empty by_type for a run with no nodes."""
    reader = _FakeReader(nodes=NodeListResult(nodes=()))
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}/count")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["by_type"] == []


def test_edge_count_by_type() -> None:
    """GET /edges/{run_id}/count returns correct total and by_type."""
    all_rels = (
        tuple(_edge(i, "KNOWS") for i in range(40))
        + tuple(_edge(i, "WORKS_FOR") for i in range(35))
    )
    rels = RelListResult(rels=all_rels)
    reader = _FakeReader(rels=rels)
    with _client(reader) as c:
        resp = c.get(f"/graph/edges/{_RUN_ID}/count")
    _clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 75
    types = {item["type"]: item["count"] for item in data["by_type"]}
    assert types["KNOWS"] == 40
    assert types["WORKS_FOR"] == 35


# ---------------------------------------------------------------------------
# Error handling — Neo4j unavailable
# ---------------------------------------------------------------------------


def test_node_list_neo4j_error_returns_503() -> None:
    """GET /nodes/{run_id} returns 503 when the graph reader raises."""
    reader = _FakeReader(read_error=RuntimeError("Neo4j connection refused"))
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}")
    _clear()

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "error"
    assert any(e["code"] == "neo4j_unavailable" for e in data["errors"])


def test_node_count_neo4j_error_returns_503() -> None:
    """GET /nodes/{run_id}/count returns 503 when the graph reader raises."""
    reader = _FakeReader(read_error=RuntimeError("connection timeout"))
    with _client(reader) as c:
        resp = c.get(f"/graph/nodes/{_RUN_ID}/count")
    _clear()

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "error"
    assert any(e["code"] == "neo4j_unavailable" for e in data["errors"])


def test_edge_list_neo4j_error_returns_503() -> None:
    """GET /edges/{run_id} returns 503 when the graph reader raises."""
    reader = _FakeReader(read_error=RuntimeError("cluster unavailable"))
    with _client(reader) as c:
        resp = c.get(f"/graph/edges/{_RUN_ID}")
    _clear()

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "error"
    assert any(e["code"] == "neo4j_unavailable" for e in data["errors"])


def test_edge_count_neo4j_error_returns_503() -> None:
    """GET /edges/{run_id}/count returns 503 when the graph reader raises."""
    reader = _FakeReader(read_error=RuntimeError("driver not open"))
    with _client(reader) as c:
        resp = c.get(f"/graph/edges/{_RUN_ID}/count")
    _clear()

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "error"
    assert any(e["code"] == "neo4j_unavailable" for e in data["errors"])
