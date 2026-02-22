"""
api/schema/models.py — Domain schema Pydantic models (SPEC-02 S-02.1).

Defines the three core schema types:

  - NodeTypeDef:    describes a node label/class with its property contract
  - EdgeTypeDef:    describes a relationship type with start/end node constraints
  - SchemaVersion:  immutable wrapper with a deterministic version_hash

All three models are frozen.  NodeTypeDef and EdgeTypeDef use frozen=True so
they are safe to embed in the immutable SchemaVersion tuple fields; any
post-construction mutation attempt raises a Pydantic ValidationError.

version_hash is a @computed_field property on SchemaVersion — it is never
accepted as input and is always re-derived from the canonical sorted content.
Because the owning model is frozen, the computed value is structurally stable
for the lifetime of the object.

Identity contract (CLAUDE.md §5):
    node_dedupe_key = (NodeType, primary_property, schema_version.version_hash)
    rel_dedupe_key  = (RelType, start_key, end_key, schema_version.version_hash)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field, field_validator


class NodeTypeDef(BaseModel):
    """Defines a single node label class in the domain schema.

    Attributes:
        node_class: High-level class or category grouping (e.g. "Entity",
            "Event").  Corresponds to the conceptual "class" in the spec.
        type: Specific Neo4j label (e.g. "Person", "Organization").
        primary_property: The identifying property used for deduplication and
            for deriving node_dedupe_key (CLAUDE.md §5).
        qualifier: Optional sub-type discriminator within the class.
        additional_properties: Supplementary tracked property names, ordered
            but not significant for identity.
    """

    model_config = ConfigDict(frozen=True)

    node_class: str
    type: str
    primary_property: str
    qualifier: str | None = None
    additional_properties: tuple[str, ...] = ()

    @field_validator("node_class", "type", "primary_property")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class EdgeTypeDef(BaseModel):
    """Defines a single relationship type in the domain schema.

    Attributes:
        start_node_type: The `type` value of the source NodeTypeDef.
        end_node_type: The `type` value of the target NodeTypeDef.
        type: Neo4j relationship type (e.g. "WORKS_FOR", "MENTIONS").
        primary_property: Optional identifying property for rel deduplication.
        qualifier: Optional sub-type discriminator.
        additional_properties: Supplementary tracked property names.
    """

    model_config = ConfigDict(frozen=True)

    start_node_type: str
    end_node_type: str
    type: str
    primary_property: str | None = None
    qualifier: str | None = None
    additional_properties: tuple[str, ...] = ()

    @field_validator("start_node_type", "end_node_type", "type")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


# ---------------------------------------------------------------------------
# Internal hash helper — not exported
# ---------------------------------------------------------------------------

def _derive_version_hash(
    nodes: tuple[NodeTypeDef, ...],
    edges: tuple[EdgeTypeDef, ...],
) -> str:
    """Return the SHA-256 hex digest of the sorted canonical schema JSON.

    Determinism guarantees:
      - Nodes sorted by (type, node_class) — stable, case-sensitive, ASCII
      - Edges sorted by (type, start_node_type, end_node_type)
      - json.dumps with sort_keys=True eliminates dict-key order variance
      - compact separators (",", ":") eliminate whitespace variance
      - ensure_ascii=True eliminates encoding variance
      - UTF-8 encoding of the final string is byte-stable

    Same schema content always produces the same hexdigest regardless of
    insertion order, Python object identity, or process restart.
    """
    canonical: dict[str, Any] = {
        "nodes": sorted(
            [n.model_dump() for n in nodes],
            key=lambda x: (x["type"], x["node_class"]),
        ),
        "edges": sorted(
            [e.model_dump() for e in edges],
            key=lambda x: (x["type"], x["start_node_type"], x["end_node_type"]),
        ),
    }
    serialized = json.dumps(
        canonical,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# SchemaVersion — the governed, immutable schema snapshot
# ---------------------------------------------------------------------------

class SchemaVersion(BaseModel):
    """Immutable, deterministically-versioned schema snapshot.

    Construction is the only moment at which content can be supplied.
    After construction, all fields — including the computed version_hash —
    are structurally locked (frozen=True raises on any attempted mutation).

    version_hash is a @computed_field property: it is excluded from
    constructor input but included in .model_dump() and .model_dump_json()
    serialisation, making it available to callers that cache or transmit the
    schema (e.g. CacheKey.schema(run_id) stores the full SchemaVersion).

    Usage:
        sv = SchemaVersion(run_id=run_id, nodes=(node_a, node_b), edges=(edge_x,))
        sv.version_hash   # → deterministic 64-char hex string
        sv.model_dump()   # → includes version_hash in the dict

    Post-lock contract (enforced by schema service, not this model):
        Once a SchemaVersion is stored as the approved schema for a run,
        no replacement is permitted — the service raises SchemaLockedError.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    nodes: tuple[NodeTypeDef, ...]
    edges: tuple[EdgeTypeDef, ...]

    @field_validator("run_id")
    @classmethod
    def _run_id_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("run_id must be a non-empty string")
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def version_hash(self) -> str:
        """SHA-256 hex digest of the sorted canonical JSON representation.

        Always derived from `nodes` and `edges`; never accepted as input.
        Because this model is frozen, the hash is stable for the object's
        lifetime — content cannot change after __init__.
        """
        return _derive_version_hash(self.nodes, self.edges)
