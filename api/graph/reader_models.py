"""
api/graph/reader_models.py — Pydantic result models for GraphReader (SPEC-05 S-05.3).

All read query results are typed Pydantic models — no raw dicts cross module
boundaries (SKILL-A R-A3).  Properties dicts contain only JSON-serialisable
primitives after _serialize_value() conversion in reader.py.

Frozen so results are safe to cache and share across callers.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class GraphNodeRecord(BaseModel):
    """A single node returned by a read query.

    properties contains only JSON-serialisable primitives (str, int, float,
    bool, list, None).  Neo4j-specific types are converted at fetch time.
    The dict attribute is mutable by value — treat as read-only after
    construction (frozen=True prevents field reassignment, not dict mutation).
    """

    model_config = ConfigDict(frozen=True)

    dedupe_key: str
    node_type: str
    run_id: str
    schema_version: str
    properties: dict[str, Any]


class GraphRelRecord(BaseModel):
    """A single directed relationship returned by a read query."""

    model_config = ConfigDict(frozen=True)

    dedupe_key: str
    rel_type: str
    start_dedupe_key: str
    end_dedupe_key: str
    run_id: str
    schema_version: str
    properties: dict[str, Any]


class NodeListResult(BaseModel):
    """Cacheable container for a list of GraphNodeRecord results."""

    model_config = ConfigDict(frozen=True)

    nodes: tuple[GraphNodeRecord, ...]


class RelListResult(BaseModel):
    """Cacheable container for a list of GraphRelRecord results."""

    model_config = ConfigDict(frozen=True)

    rels: tuple[GraphRelRecord, ...]


class NeighborResult(BaseModel):
    """Cacheable container for the neighbor set of a single node."""

    model_config = ConfigDict(frozen=True)

    node_dedupe_key: str
    neighbors: tuple[GraphNodeRecord, ...]


class OrphanResult(BaseModel):
    """Cacheable container for orphan (degree-zero) node dedupe_keys."""

    model_config = ConfigDict(frozen=True)

    orphan_dedupe_keys: tuple[str, ...]


class DegreeRecord(BaseModel):
    """In/out degree for a single node."""

    model_config = ConfigDict(frozen=True)

    dedupe_key: str
    in_degree: int
    out_degree: int


class DegreeListResult(BaseModel):
    """Cacheable container for per-node degree values for an entire run."""

    model_config = ConfigDict(frozen=True)

    degrees: tuple[DegreeRecord, ...]
