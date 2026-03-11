"""
api/models/extraction.py — Extraction domain models (SPEC-04 S-04.3).

Defines the governed, immutable artifacts produced by the AI-assisted extraction
phase (Phase 3). These types flow from ExtractionService → graph writer
(api/graph/writer.py) for MERGE into Neo4j.

Models:
    ExtractedNode:    A single entity extracted from a chunk; carries provenance
                      (run_id, chunk_id, schema_version) and a deterministic
                      node_dedupe_key used by the graph writer's MERGE operation.
    ExtractedEdge:    A single relationship extracted from a chunk; carries
                      provenance and a deterministic rel_dedupe_key.
    ExtractionResult: Top-level container for one chunk's extraction output.

Identity contract (CLAUDE.md §5):
    node_dedupe_key = (NodeType, primary_value, schema_version)
    rel_dedupe_key  = (RelType, start_key, end_key, schema_version)
        where start_key = "<start_node_type>|<start_primary_value>|<schema_version>"
              end_key   = "<end_node_type>|<end_primary_value>|<schema_version>"

schema_version is the version_hash of the locked SchemaVersion — never uuid4().
It references the deterministic SHA-256 hash computed by api/schema/models.py.

Immutability:
    All three models are frozen (ConfigDict(frozen=True)). node_dedupe_key and
    rel_dedupe_key are @computed_field properties derived from stored fields and
    are never accepted as constructor input. Because the models are frozen, the
    computed values are stable for each object's lifetime.

Provenance:
    Every ExtractedNode and ExtractedEdge carries run_id, chunk_id, and
    schema_version so each artifact is fully self-describing. chunk_id links
    back to the ingested Chunk (api/models/document.py) from which the entity
    was extracted.

Sensitive data (SKILL-D R-D5):
    chunk_id is the safe identifier to log. The chunk text itself is never
    logged — only passed to the LLM inside the service.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


# ---------------------------------------------------------------------------
# ExtractedNode
# ---------------------------------------------------------------------------

class ExtractedNode(BaseModel):
    """A single entity extracted from a document chunk by the LLM.

    node_dedupe_key is a @computed_field tuple used by the graph writer's
    MERGE operation to identify existing nodes without creating duplicates on
    re-run. It is never accepted as constructor input.

    Identity contract (CLAUDE.md §5):
        node_dedupe_key = (node_type, primary_value, schema_version)

    Attributes:
        run_id:          Governed run this extraction belongs to.
        chunk_id:        Source chunk (provenance — links to Chunk.chunk_id).
        schema_version:  version_hash of the locked SchemaVersion. References
                         the schema under which extraction was performed.
                         Never uuid4() — always the deterministic SHA-256 hash
                         computed by SchemaVersion.version_hash.
        node_type:       Neo4j label. Must match a NodeTypeDef.type in the
                         locked schema (e.g. "Person", "Organization").
                         Validated against the locked schema by ExtractionService
                         before this model is constructed.
        primary_value:   Extracted value of NodeTypeDef.primary_property.
                         E.g. "John Smith" for a Person with primary_property
                         "name". Participates in node_dedupe_key derivation.
        properties:      Additional extracted property key-value pairs.
                         Keys should correspond to NodeTypeDef.additional_properties.
                         The model attribute cannot be reassigned (frozen=True),
                         but dict contents are mutable by value — treat as
                         read-only after construction.
        node_dedupe_key: Computed deterministic MERGE identity tuple (read-only).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    chunk_id: str
    schema_version: str
    node_type: str
    primary_value: str
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id", "chunk_id", "schema_version", "node_type", "primary_value")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        import unicodedata

        cleaned = "".join(
            " " if (unicodedata.category(ch).startswith("Z") and ch != " ") else ch
            for ch in v
        ).strip()
        if not cleaned:
            raise ValueError("must be a non-empty, non-whitespace string")
        return cleaned

    @computed_field  # type: ignore[prop-decorator]
    @property
    def node_dedupe_key(self) -> tuple[str, str, str]:
        """Deterministic MERGE identity: (node_type, primary_value, schema_version).

        Same inputs always produce the same tuple. Used by the graph writer
        to MERGE nodes without creating duplicates across runs.
        Never accepted as constructor input.
        """
        return (self.node_type, self.primary_value, self.schema_version)


# ---------------------------------------------------------------------------
# ExtractedEdge
# ---------------------------------------------------------------------------

class ExtractedEdge(BaseModel):
    """A single relationship extracted from a document chunk by the LLM.

    rel_dedupe_key is a @computed_field tuple used by the graph writer's
    MERGE operation. start_key and end_key within it are pipe-joined string
    representations of the endpoint node dedupe keys — deterministic and
    stable across process restarts.

    The pipe separator ("|") is chosen because it does not appear in
    schema version hashes (hex digits only) and is not expected in Neo4j
    node type names or property values in this domain.

    Identity contract (CLAUDE.md §5):
        rel_dedupe_key = (rel_type, start_key, end_key, schema_version)
        where:
          start_key = "|".join((start_node_type, start_primary_value, schema_version))
          end_key   = "|".join((end_node_type,   end_primary_value,   schema_version))

    Attributes:
        run_id:              Governed run this extraction belongs to.
        chunk_id:            Source chunk (provenance linkage).
        schema_version:      version_hash of the locked SchemaVersion.
        rel_type:            Neo4j relationship type. Must match an EdgeTypeDef.type
                             in the locked schema. E.g. "WORKS_FOR", "MENTIONS".
        start_node_type:     Neo4j label of the source node. E.g. "Person".
        start_primary_value: Extracted primary value of the source node.
        end_node_type:       Neo4j label of the target node. E.g. "Organization".
        end_primary_value:   Extracted primary value of the target node.
        properties:          Additional extracted relationship property key-value pairs.
                             The model attribute cannot be reassigned (frozen=True),
                             but dict contents are mutable by value — treat as
                             read-only after construction.
        rel_dedupe_key:      Computed deterministic MERGE identity tuple (read-only).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    chunk_id: str
    schema_version: str
    rel_type: str
    start_node_type: str
    start_primary_value: str
    end_node_type: str
    end_primary_value: str
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "run_id",
        "chunk_id",
        "schema_version",
        "rel_type",
        "start_node_type",
        "start_primary_value",
        "end_node_type",
        "end_primary_value",
    )
    @classmethod
    def _non_empty(cls, v: str) -> str:
        import unicodedata

        cleaned = "".join(
            " " if (unicodedata.category(ch).startswith("Z") and ch != " ") else ch
            for ch in v
        ).strip()
        if not cleaned:
            raise ValueError("must be a non-empty, non-whitespace string")
        return cleaned

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rel_dedupe_key(self) -> tuple[str, str, str, str]:
        """Deterministic MERGE identity: (rel_type, start_key, end_key, schema_version).

        start_key and end_key are pipe-joined node dedupe key strings so that
        each endpoint is fully identified without nesting tuples:
            "<node_type>|<primary_value>|<schema_version>"

        Never accepted as constructor input.
        """
        start_key = "|".join(
            (self.start_node_type, self.start_primary_value, self.schema_version)
        )
        end_key = "|".join(
            (self.end_node_type, self.end_primary_value, self.schema_version)
        )
        return (self.rel_type, start_key, end_key, self.schema_version)


# ---------------------------------------------------------------------------
# ExtractionResult
# ---------------------------------------------------------------------------

class ExtractionResult(BaseModel):
    """Top-level extraction output for a single document chunk.

    Produced by ExtractionService after LLM output is validated against the
    locked schema and each entity is promoted to a governed domain model.
    Carries provenance for the entire extraction event.

    All nodes and edges within this result share the same run_id, chunk_id,
    and schema_version. These fields are stamped by the service, not supplied
    by the LLM.

    An empty nodes/edges tuple is valid — not every chunk produces extractable
    entities (e.g. a table of contents or a blank page).

    Attributes:
        run_id:         Governed run this extraction belongs to.
        chunk_id:       Source chunk from which nodes/edges were extracted.
        schema_version: version_hash of the locked schema used during extraction.
        nodes:          Immutable tuple of extracted nodes (may be empty).
        edges:          Immutable tuple of extracted edges (may be empty).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    chunk_id: str
    schema_version: str
    nodes: tuple[ExtractedNode, ...] = ()
    edges: tuple[ExtractedEdge, ...] = ()

    @field_validator("run_id", "chunk_id", "schema_version")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v
