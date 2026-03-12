"""
api/routers/graph_explorer_models.py — Pydantic models and pagination helpers
for the graph explorer endpoints (SPEC-06 S-06.7).

Split from graph_explorer.py per SKILL-B R-B7 (>400 lines → split).

Models:
    NodeOut, EdgeOut, TypeCount — serialisable element representations
    NodePageResponse, EdgePageResponse — paginated list responses
    NodeCountResponse, EdgeCountResponse — count/breakdown responses

Helpers:
    _clamp_page_size, _clamp_page — input clamping for pagination params
    _MAX_PAGE_SIZE, _DEFAULT_PAGE_SIZE — pagination constants
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from api.models.responses import BaseResponse

_MAX_PAGE_SIZE: int = 5000
_DEFAULT_PAGE_SIZE: int = 50


# ---------------------------------------------------------------------------
# Serialisable element models
# ---------------------------------------------------------------------------


class NodeOut(BaseModel):
    """Serialisable representation of a graph node for API responses.

    properties contains only JSON-serialisable primitives (the GraphReader
    converts all Neo4j-specific types at read time).
    """

    dedupe_key: str
    node_type: str
    run_id: str
    schema_version: str
    properties: dict[str, Any]


class EdgeOut(BaseModel):
    """Serialisable representation of a directed graph edge for API responses."""

    dedupe_key: str
    rel_type: str
    start_dedupe_key: str
    end_dedupe_key: str
    run_id: str
    schema_version: str
    properties: dict[str, Any]


class TypeCount(BaseModel):
    """Count of elements for a single type label."""

    type: str
    count: int


# ---------------------------------------------------------------------------
# Response models (SKILL-A R-A1, R-A2)
# ---------------------------------------------------------------------------


class NodePageResponse(BaseResponse):
    """Response for GET /nodes/{run_id}.

    Attributes:
        run_id:      The governed run being browsed.
        page:        1-indexed current page number.
        page_size:   Number of items per page (≤ 5000).
        total:       Total matching nodes (after type filtering, before paging).
        has_more:    True if further pages exist.
        items:       Nodes on this page, sorted by dedupe_key.
    """

    run_id: str = ""
    page: int = 1
    page_size: int = _DEFAULT_PAGE_SIZE
    total: int = 0
    has_more: bool = False
    items: list[NodeOut] = []


class EdgePageResponse(BaseResponse):
    """Response for GET /edges/{run_id}.

    Attributes:
        run_id:      The governed run being browsed.
        page:        1-indexed current page number.
        page_size:   Number of items per page (≤ 5000).
        total:       Total matching edges (after type filtering, before paging).
        has_more:    True if further pages exist.
        items:       Edges on this page, sorted by dedupe_key.
    """

    run_id: str = ""
    page: int = 1
    page_size: int = _DEFAULT_PAGE_SIZE
    total: int = 0
    has_more: bool = False
    items: list[EdgeOut] = []


class NodeCountResponse(BaseResponse):
    """Response for GET /nodes/{run_id}/count.

    Attributes:
        run_id:   The governed run.
        total:    Total node count across all types.
        by_type:  Per-type count, sorted descending by count.
    """

    run_id: str = ""
    total: int = 0
    by_type: list[TypeCount] = []


class EdgeCountResponse(BaseResponse):
    """Response for GET /edges/{run_id}/count.

    Attributes:
        run_id:   The governed run.
        total:    Total edge count across all relationship types.
        by_type:  Per-type count, sorted descending by count.
    """

    run_id: str = ""
    total: int = 0
    by_type: list[TypeCount] = []


# ---------------------------------------------------------------------------
# Pagination helpers
# ---------------------------------------------------------------------------


def _clamp_page_size(requested: int) -> int:
    """Clamp page_size to [1, _MAX_PAGE_SIZE]."""
    return max(1, min(requested, _MAX_PAGE_SIZE))


def _clamp_page(requested: int) -> int:
    """Clamp page to >= 1."""
    return max(1, requested)
