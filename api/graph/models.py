"""
api/graph/models.py — Typed result models for graph write operations (SPEC-04).

These models satisfy SKILL-A R-A3: no raw dicts cross module boundaries between
api/graph/ and its callers (api/services/extraction.py, api/worker/jobs.py).

All models are frozen. action literals are exhaustive — callers can match on
them without a default branch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class NodeWriteResult(BaseModel):
    """Result of a single MERGE node operation in GraphWriter."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    chunk_id: str
    node_type: str
    dedupe_key: str
    action: Literal["merge_created", "merge_matched"]


class EdgeWriteResult(BaseModel):
    """Result of a single MERGE edge operation in GraphWriter.

    action="skipped" means the MATCH for one or both endpoint nodes returned
    zero rows. This is expected when the upstream node write failed or was
    called out of order.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    chunk_id: str
    rel_type: str
    dedupe_key: str
    action: Literal["merge_created", "merge_matched", "skipped"]


class ExtractionWriteResult(BaseModel):
    """Aggregated result of writing one full ExtractionResult to the graph."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    chunk_id: str
    schema_version: str
    nodes_created: int
    nodes_matched: int
    edges_created: int
    edges_matched: int
    edges_skipped: int
    node_results: tuple[NodeWriteResult, ...]
    edge_results: tuple[EdgeWriteResult, ...]
