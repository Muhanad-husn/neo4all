"""
api/routers/schema.py — POST /api/schema/propose, POST /api/schema/approve,
GET /api/schema/{run_id}  (SPEC-02 S-02.4).

Three-phase schema lifecycle for a run:
  1. propose — LLM generates candidate node/edge types for human review.
  2. approve  — user-edited proposal is validated, frozen, and cached.
  3. get      — retrieve the locked SchemaVersion (cache-first, no Neo4j).

Architecture
------------
All business logic is delegated to SchemaService (SKILL-B: thin routers).
Request/response models are defined in api/routers/schema_models.py (SKILL-A).
SchemaService is injected via FastAPI's Depends() mechanism.

Error handling
--------------
SchemaLockedError   → 409 Conflict — schema already locked for this run.
LLMProposalError    → 422 Unprocessable — LLM returned invalid output.
pydantic.ValidationError → 422 Unprocessable — node/edge definition invalid.
Both failure cases return a structured BaseResponse (status="error", errors=[]).

Path ordering note
------------------
The static routes /propose and /approve are registered before /{run_id} so
FastAPI resolves them first and avoids a spurious parameter match.
"""

from __future__ import annotations

from pydantic import ValidationError

from fastapi import APIRouter, Depends, Response

from api.models.responses import ErrorDetail
from api.observability.logger import get_logger
from api.routers.schema_models import (
    AmendSchemaRequest,
    AmendSchemaResponse,
    ApproveSchemaRequest,
    ApproveSchemaResponse,
    EdgeTypePayload,
    GetAmendmentsResponse,
    GetSchemaResponse,
    NodeTypePayload,
    ProposeSchemaRequest,
    ProposeSchemaResponse,
    SchemaAmendmentOut,
    SchemaVersionOut,
    ValidateSchemaRequest,
    ValidateSchemaResponse,
)
from api.schema.amendments import SchemaAmendment
from api.schema.models import EdgeTypeDef, SchemaVersion
from api.schema.service import (
    EdgeAlreadyInSchemaError,
    LLMProposalError,
    SchemaLockedError,
    SchemaNotLockedError,
    SchemaProposal,
    SchemaService,
    UndefinedNodeTypeError,
    get_schema_service,
)

logger = get_logger(__name__)

router = APIRouter(tags=["schema"])


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _schema_version_to_out(sv: SchemaVersion) -> SchemaVersionOut:
    """Convert a frozen SchemaVersion to its serialisable response form.

    NodeTypeDef.additional_properties is tuple[str, ...]; SchemaVersionOut
    uses list[str] for JSON compatibility — converted here at the boundary.
    """
    return SchemaVersionOut(
        run_id=sv.run_id,
        version_hash=sv.version_hash,
        node_count=len(sv.nodes),
        edge_count=len(sv.edges),
        nodes=[
            NodeTypePayload(
                node_class=n.node_class,
                type=n.type,
                primary_property=n.primary_property,
                qualifier=n.qualifier,
                additional_properties=list(n.additional_properties),
            )
            for n in sv.nodes
        ],
        edges=[
            EdgeTypePayload(
                start_node_type=e.start_node_type,
                end_node_type=e.end_node_type,
                type=e.type,
                primary_property=e.primary_property,
                qualifier=e.qualifier,
                additional_properties=list(e.additional_properties),
            )
            for e in sv.edges
        ],
    )


# ---------------------------------------------------------------------------
# POST /api/schema/propose
# ---------------------------------------------------------------------------


@router.post(
    "/propose",
    response_model=ProposeSchemaResponse,
    summary="Generate a candidate domain schema via LLM",
    responses={
        409: {"model": ProposeSchemaResponse, "description": "Schema already locked"},
        422: {"model": ProposeSchemaResponse, "description": "LLM returned invalid output"},
    },
)
async def propose_schema(
    request: ProposeSchemaRequest,
    response: Response,
    service: SchemaService = Depends(get_schema_service),
) -> ProposeSchemaResponse:
    """Generate candidate node and edge types for a domain description.

    Loads the versioned prompt template (`prompts/schema_propose/v1.yaml`),
    calls the OpenRouter LLM, validates the structured response, and returns
    typed candidate node and edge types for human review.

    The schema is **not** locked at this stage. The caller reviews and edits
    the proposal, then submits it to `POST /api/schema/approve` to lock it.

    **Errors**
    - `409 Conflict` — a schema is already locked for this `run_id`.
    - `422 Unprocessable` — the LLM returned an invalid or empty proposal.
    """
    logger.info(
        "schema_propose_requested",
        run_id=request.run_id,
        domain_length=len(request.domain_description),
    )

    try:
        proposal: SchemaProposal = await service.propose(
            run_id=request.run_id,
            domain_description=request.domain_description,
            model_override=request.model,
        )
    except SchemaLockedError as exc:
        logger.warning(
            "schema_propose_rejected_locked",
            run_id=request.run_id,
            version_hash=exc.version_hash,
        )
        response.status_code = 409
        return ProposeSchemaResponse(
            run_id=request.run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="schema_locked",
                    message=str(exc),
                )
            ],
        )
    except LLMProposalError as exc:
        logger.error(
            "schema_propose_llm_error",
            run_id=request.run_id,
            error=str(exc),
        )
        response.status_code = 422
        return ProposeSchemaResponse(
            run_id=request.run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="llm_proposal_failed",
                    message=str(exc),
                )
            ],
        )

    nodes = [NodeTypePayload(**n.model_dump()) for n in proposal.nodes]
    edges = [EdgeTypePayload(**e.model_dump()) for e in proposal.edges]

    logger.info(
        "schema_propose_success",
        run_id=request.run_id,
        node_count=len(nodes),
        edge_count=len(edges),
    )

    return ProposeSchemaResponse(
        run_id=request.run_id,
        status="success",
        nodes=nodes,
        edges=edges,
    )


# ---------------------------------------------------------------------------
# POST /api/schema/approve
# ---------------------------------------------------------------------------


@router.post(
    "/approve",
    response_model=ApproveSchemaResponse,
    summary="Validate, freeze, and cache the domain schema for a run",
    responses={
        409: {"model": ApproveSchemaResponse, "description": "Schema already locked"},
        422: {"model": ApproveSchemaResponse, "description": "Node/edge definition invalid"},
    },
)
async def approve_schema(
    request: ApproveSchemaRequest,
    response: Response,
    service: SchemaService = Depends(get_schema_service),
) -> ApproveSchemaResponse:
    """Lock the domain schema for a run.

    Accepts the caller's `schema_data` — typically the LLM proposal from
    `POST /api/schema/propose`, possibly edited by the user. Constructs typed
    `NodeTypeDef` / `EdgeTypeDef` objects, wraps them in a deterministic
    `SchemaVersion`, and caches the result with no TTL (immutable for the
    run lifetime, SKILL-D R-D8).

    After this call succeeds, the schema is locked. Any subsequent call to
    `/propose` or `/approve` for the same `run_id` returns `409 Conflict`.

    **Errors**
    - `409 Conflict` — schema already locked for this `run_id`.
    - `422 Unprocessable` — a node or edge definition fails domain validation.
    """
    logger.info(
        "schema_approve_requested",
        run_id=request.run_id,
        node_count=len(request.schema_data.nodes),
        edge_count=len(request.schema_data.edges),
    )

    # Convert router-layer payload to the service-layer SchemaProposal.
    # model_validate() from dicts avoids importing private _RawNodeType /
    # _RawEdgeType names from the service module.
    schema_proposal = SchemaProposal.model_validate({
        "nodes": [n.model_dump() for n in request.schema_data.nodes],
        "edges": [e.model_dump() for e in request.schema_data.edges],
    })

    try:
        schema_version: SchemaVersion = await service.approve(
            run_id=request.run_id,
            schema_data=schema_proposal,
        )
    except SchemaLockedError as exc:
        logger.warning(
            "schema_approve_rejected_locked",
            run_id=request.run_id,
            version_hash=exc.version_hash,
        )
        response.status_code = 409
        return ApproveSchemaResponse(
            run_id=request.run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="schema_locked",
                    message=str(exc),
                )
            ],
        )
    except ValidationError as exc:
        # NodeTypeDef / EdgeTypeDef construction inside service.approve()
        # raises ValidationError on invalid field values — fail-closed
        # (CLAUDE.md §4.4).
        logger.error(
            "schema_approve_validation_failed",
            run_id=request.run_id,
            error=str(exc),
        )
        response.status_code = 422
        return ApproveSchemaResponse(
            run_id=request.run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="schema_validation_failed",
                    message=str(exc),
                )
            ],
        )

    logger.info(
        "schema_approve_success",
        run_id=request.run_id,
        version_hash=schema_version.version_hash,
    )

    return ApproveSchemaResponse(
        run_id=request.run_id,
        status="success",
        schema_version=_schema_version_to_out(schema_version),
    )


# ---------------------------------------------------------------------------
# POST /api/schema/validate
# ---------------------------------------------------------------------------


@router.post(
    "/validate",
    response_model=ValidateSchemaResponse,
    summary="Validate a user-supplied schema without LLM involvement",
)
async def validate_schema(
    request: ValidateSchemaRequest,
    service: SchemaService = Depends(get_schema_service),
) -> ValidateSchemaResponse:
    """Validate structural and semantic correctness of a pre-prepared schema.

    Performs all checks that the approval pipeline would perform, plus
    additional semantic checks (naming conventions, orphan nodes, count
    limits, dangling edge references). Returns issues in human-readable form.

    When the response has `valid=True`, the schema can be submitted directly
    to `POST /api/schema/approve` without modification.
    """
    logger.info(
        "schema_validate_requested",
        run_id=request.run_id,
        node_count=len(request.schema_data.nodes),
        edge_count=len(request.schema_data.edges),
    )

    schema_proposal = SchemaProposal.model_validate({
        "nodes": [n.model_dump() for n in request.schema_data.nodes],
        "edges": [e.model_dump() for e in request.schema_data.edges],
    })

    issues: list[str] = await service.validate(
        run_id=request.run_id,
        schema_data=schema_proposal,
    )

    if issues:
        logger.info(
            "schema_validate_issues_found",
            run_id=request.run_id,
            issue_count=len(issues),
        )
        return ValidateSchemaResponse(
            run_id=request.run_id,
            status="success",
            valid=False,
            issues=issues,
            nodes=list(request.schema_data.nodes),
            edges=list(request.schema_data.edges),
        )

    logger.info(
        "schema_validate_success",
        run_id=request.run_id,
        node_count=len(request.schema_data.nodes),
        edge_count=len(request.schema_data.edges),
    )
    return ValidateSchemaResponse(
        run_id=request.run_id,
        status="success",
        valid=True,
        nodes=list(request.schema_data.nodes),
        edges=list(request.schema_data.edges),
    )


# ---------------------------------------------------------------------------
# POST /api/schema/amend
# ---------------------------------------------------------------------------


def _amendment_to_out(a: SchemaAmendment) -> SchemaAmendmentOut:
    """Convert an internal SchemaAmendment to its serialisable response form."""
    e = a.edge_type_def
    return SchemaAmendmentOut(
        amendment_id=a.amendment_id,
        run_id=a.run_id,
        edge_type=EdgeTypePayload(
            start_node_type=e.start_node_type,
            end_node_type=e.end_node_type,
            type=e.type,
            primary_property=e.primary_property,
            qualifier=e.qualifier,
            additional_properties=list(e.additional_properties),
        ),
        source_candidate_id=a.source_candidate_id,
        actor=a.actor,
        timestamp_utc=a.timestamp_utc,
        rationale=a.rationale,
    )


@router.post(
    "/amend",
    response_model=AmendSchemaResponse,
    summary="Add a new edge type to the schema as an additive amendment",
    responses={
        404: {"model": AmendSchemaResponse, "description": "Schema not locked"},
        409: {"model": AmendSchemaResponse, "description": "Edge already in base schema"},
        422: {"model": AmendSchemaResponse, "description": "Edge references undefined node types"},
    },
)
async def amend_schema(
    request: AmendSchemaRequest,
    response: Response,
    service: SchemaService = Depends(get_schema_service),
) -> AmendSchemaResponse:
    """Accept a canonical violation as valid by adding the missing edge direction.

    This adds the new EdgeTypeDef to a supplementary amendment layer without
    modifying the locked SchemaVersion or its version_hash.  All similar
    canonical violations disappear on the next candidate detection run.

    **Errors**
    - ``404 Not Found`` — schema is not yet locked for this run_id.
    - ``409 Conflict`` — the edge already exists in the base schema.
    - ``422 Unprocessable`` — an edge endpoint references an undefined node type.
    """
    logger.info(
        "schema_amend_requested",
        run_id=request.run_id,
        edge_type=request.edge_type.type,
        start_node_type=request.edge_type.start_node_type,
        end_node_type=request.edge_type.end_node_type,
    )

    new_edge = EdgeTypeDef(
        start_node_type=request.edge_type.start_node_type,
        end_node_type=request.edge_type.end_node_type,
        type=request.edge_type.type,
        primary_property=request.edge_type.primary_property,
        qualifier=request.edge_type.qualifier,
        additional_properties=tuple(request.edge_type.additional_properties),
    )

    try:
        amendment = await service.amend_edge(
            run_id=request.run_id,
            new_edge=new_edge,
            source_candidate_id=request.source_candidate_id,
            actor=request.actor,
            rationale=request.rationale,
        )
    except SchemaNotLockedError as exc:
        logger.warning("schema_amend_not_locked", run_id=request.run_id)
        response.status_code = 404
        return AmendSchemaResponse(
            run_id=request.run_id,
            status="error",
            errors=[
                ErrorDetail(code="schema_not_locked", message=str(exc))
            ],
        )
    except EdgeAlreadyInSchemaError as exc:
        logger.info("schema_amend_already_in_base", run_id=request.run_id)
        response.status_code = 409
        return AmendSchemaResponse(
            run_id=request.run_id,
            status="error",
            errors=[
                ErrorDetail(code="edge_already_in_schema", message=str(exc))
            ],
        )
    except UndefinedNodeTypeError as exc:
        logger.warning("schema_amend_undefined_node", run_id=request.run_id)
        response.status_code = 422
        return AmendSchemaResponse(
            run_id=request.run_id,
            status="error",
            errors=[
                ErrorDetail(code="undefined_node_type", message=str(exc))
            ],
        )

    logger.info(
        "schema_amend_success",
        run_id=request.run_id,
        amendment_id=amendment.amendment_id,
    )
    return AmendSchemaResponse(
        run_id=request.run_id,
        status="success",
        amendment=_amendment_to_out(amendment),
    )


# ---------------------------------------------------------------------------
# GET /api/schema/{run_id}/amendments
# ---------------------------------------------------------------------------


@router.get(
    "/{run_id}/amendments",
    response_model=GetAmendmentsResponse,
    summary="Return the amendment history for a run",
)
async def get_amendments(
    run_id: str,
    service: SchemaService = Depends(get_schema_service),
) -> GetAmendmentsResponse:
    """Retrieve all schema amendments for a run.

    Returns an empty list if no amendments have been made.
    """
    logger.info("schema_amendments_requested", run_id=run_id)

    registry = await service.get_amendments(run_id)
    if registry is None or not registry.amendments:
        return GetAmendmentsResponse(
            run_id=run_id,
            status="success",
            amendments=[],
        )

    return GetAmendmentsResponse(
        run_id=run_id,
        status="success",
        amendments=[_amendment_to_out(a) for a in registry.amendments],
    )


# ---------------------------------------------------------------------------
# GET /api/schema/{run_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{run_id}",
    response_model=GetSchemaResponse,
    summary="Return the locked schema for a run, or indicate not-yet-locked",
)
async def get_schema(
    run_id: str,
    service: SchemaService = Depends(get_schema_service),
) -> GetSchemaResponse:
    """Retrieve the current schema state for a run.

    Cache-first: reads from Redis. A cache miss is authoritative — if the
    schema is absent it has not been approved for this run. No Neo4j query
    is issued (CLAUDE.md §12 — schemas are never persisted to the graph).

    Returns `locked=False` (not an error, `status="success"`) when the schema
    has not yet been approved. Returns `locked=True` with the full
    `SchemaVersionOut` when it has.
    """
    logger.info("schema_get_requested", run_id=run_id)

    schema_version: SchemaVersion | None = await service.get_current(run_id=run_id)

    if schema_version is None:
        logger.debug("schema_get_not_locked", run_id=run_id)
        return GetSchemaResponse(
            run_id=run_id,
            status="success",
            locked=False,
        )

    logger.info(
        "schema_get_found",
        run_id=run_id,
        version_hash=schema_version.version_hash,
    )

    return GetSchemaResponse(
        run_id=run_id,
        status="success",
        locked=True,
        schema_version=_schema_version_to_out(schema_version),
    )
