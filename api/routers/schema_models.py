"""
api/routers/schema_models.py — Pydantic request/response models for schema
endpoints (SKILL-A R-A1, R-A2, R-A5).

Three endpoint contracts are defined here and co-located with the route
handler (SKILL-A R-A5):

  POST /api/schema/propose  →  ProposeSchemaRequest  / ProposeSchemaResponse
  POST /api/schema/approve  →  ApproveSchemaRequest  / ApproveSchemaResponse
  GET  /api/schema/{run_id} →  (path param only)     / GetSchemaResponse

Shared payload types
--------------------
  NodeTypePayload   — serialisable node type definition (request and response).
                      Mirrors NodeTypeDef but uses list[str] for
                      additional_properties (JSON-friendly; NodeTypeDef uses
                      tuple[str, ...]).
  EdgeTypePayload   — serialisable edge type definition (request and response).
  SchemaDataPayload — container for nodes + edges in the approve request body.
  SchemaVersionOut  — serialisable locked-schema snapshot for responses.

Error contract
--------------
All response models extend BaseResponse so every caller receives the standard
run_id / status / errors envelope (SKILL-A R-A2). Payload fields on response
models default to empty / None so that structured error responses can be
returned through the same typed model without carrying meaningless data.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from api.models.responses import BaseResponse


# ---------------------------------------------------------------------------
# Shared payload types — node and edge definitions
# ---------------------------------------------------------------------------


class NodeTypePayload(BaseModel):
    """Serialisable node type definition — used in both request and response.

    Attributes:
        node_class:            High-level class grouping (e.g. "Entity").
        type:                  Neo4j label (e.g. "Person").
        primary_property:      Identifying property used for deduplication.
        qualifier:             Optional sub-type discriminator.
        additional_properties: Supplementary tracked property names.
    """

    node_class: str
    type: str
    primary_property: str
    qualifier: str | None = None
    additional_properties: list[str] = []

    @field_validator("node_class", "type", "primary_property")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class EdgeTypePayload(BaseModel):
    """Serialisable edge type definition — used in both request and response.

    Attributes:
        start_node_type:       The `type` of the source NodeTypeDef.
        end_node_type:         The `type` of the target NodeTypeDef.
        type:                  Neo4j relationship type (e.g. "WORKS_FOR").
        primary_property:      Optional identifying property for deduplication.
        qualifier:             Optional sub-type discriminator.
        additional_properties: Supplementary tracked property names.
    """

    start_node_type: str
    end_node_type: str
    type: str
    primary_property: str | None = None
    qualifier: str | None = None
    additional_properties: list[str] = []

    @field_validator("start_node_type", "end_node_type", "type")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class SchemaDataPayload(BaseModel):
    """Container for a complete set of node and edge type definitions.

    Used as the schema_data field in ApproveSchemaRequest. Accepts a
    (possibly user-edited) proposal before it is validated and locked.
    """

    nodes: list[NodeTypePayload]
    edges: list[EdgeTypePayload]

    @field_validator("nodes")
    @classmethod
    def _at_least_one_node(cls, v: list[NodeTypePayload]) -> list[NodeTypePayload]:
        if not v:
            raise ValueError("schema_data must define at least one node type")
        return v


class SchemaVersionOut(BaseModel):
    """Serialisable representation of a locked SchemaVersion.

    Returned by both the approve and get endpoints so callers receive the
    deterministic version_hash alongside the full type definitions.

    Attributes:
        run_id:       Governed run identifier.
        version_hash: SHA-256 hex digest of the sorted canonical schema JSON.
        node_count:   Number of NodeTypeDef entries.
        edge_count:   Number of EdgeTypeDef entries.
        nodes:        Full node type definitions.
        edges:        Full edge type definitions.
    """

    run_id: str
    version_hash: str
    node_count: int
    edge_count: int
    nodes: list[NodeTypePayload]
    edges: list[EdgeTypePayload]


# ---------------------------------------------------------------------------
# POST /api/schema/propose
# ---------------------------------------------------------------------------


class ProposeSchemaRequest(BaseModel):
    """Request body for POST /api/schema/propose.

    Attributes:
        run_id:             Governed run identifier.
        domain_description: Free-text domain description forwarded to the LLM.
    """

    run_id: str
    domain_description: str

    @field_validator("run_id", "domain_description")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class ProposeSchemaResponse(BaseResponse):
    """Response for POST /api/schema/propose.

    On success: status="success", nodes and edges carry the LLM candidate.
    On error:   status="error", errors describe the failure; nodes/edges=[].
    """

    nodes: list[NodeTypePayload] = []
    edges: list[EdgeTypePayload] = []


# ---------------------------------------------------------------------------
# POST /api/schema/approve
# ---------------------------------------------------------------------------


class ApproveSchemaRequest(BaseModel):
    """Request body for POST /api/schema/approve.

    Attributes:
        run_id:      Governed run identifier.
        schema_data: Complete node and edge definitions to validate and lock.
                     Typically the LLM proposal, possibly user-edited.
    """

    run_id: str
    schema_data: SchemaDataPayload

    @field_validator("run_id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


class ApproveSchemaResponse(BaseResponse):
    """Response for POST /api/schema/approve.

    On success: status="success", schema_version carries the locked snapshot.
    On error:   status="error", errors describe the failure; schema_version=None.
    """

    schema_version: SchemaVersionOut | None = None


# ---------------------------------------------------------------------------
# GET /api/schema/{run_id}
# ---------------------------------------------------------------------------


class GetSchemaResponse(BaseResponse):
    """Response for GET /api/schema/{run_id}.

    Attributes:
        locked:         True when a schema has been approved for this run.
        schema_version: The locked snapshot; None when locked=False.
    """

    locked: bool
    schema_version: SchemaVersionOut | None = None
