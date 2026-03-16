"""
api/schema/service.py — Schema lifecycle service (SPEC-02 S-02.2).

Implements the three-phase schema lifecycle for a run:

  1. propose()     — calls the LLM to generate candidate node/edge types
  2. approve()     — validates, freezes, and caches the caller-supplied schema
  3. get_current() — returns the locked SchemaVersion (cache-first)

Post-lock immutability
----------------------
Once approve() succeeds for a run_id, subsequent calls to propose() or
approve() for the same run_id raise SchemaLockedError.  The lock is enforced
by checking for a cached schema before delegating to the LLM or persisting
any new data.

Cache integration (SKILL-D R-D8)
---------------------------------
Locked schemas are cached with NO TTL — they are immutable for the lifetime
of the run.  Cache misses fall through gracefully; a missing cache entry does
not indicate an unlocked schema.  The authoritative lock state is the presence
of a valid cached SchemaVersion.

  CacheKey.schema(run_id) → SchemaVersion | None

Prompt loading
--------------
propose() loads its prompt from prompts/schema_propose/v1.yaml.  No inline
prompt strings are permitted (CLAUDE.md §14).

LLM response model
------------------
The LLM is expected to return JSON matching SchemaProposal — a flat structure
containing lists of raw node and edge type dicts that this service promotes
into typed NodeTypeDef / EdgeTypeDef / SchemaVersion objects.  If the LLM
response fails validation, propose() raises LLMProposalError (fail-closed).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.observability.logger import get_logger
from api.schema.models import EdgeTypeDef, NodeTypeDef, SchemaVersion
from api.services.llm import JobConfig, LLMClient

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Prompt template resolution
# ---------------------------------------------------------------------------

_PROMPTS_ROOT = (
    Path(os.environ["PROMPTS_DIR"])
    if "PROMPTS_DIR" in os.environ
    else Path(__file__).resolve().parents[2] / "prompts"
)


def _load_prompt_template(job_id: str, template_version: str) -> dict[str, Any]:
    """Load and parse a YAML prompt template.

    Path: prompts/{job_id}/{template_version}.yaml

    Raises:
        FileNotFoundError: If the template file does not exist.
        ValueError: If the YAML is malformed or missing required keys.
    """
    path = _PROMPTS_ROOT / job_id / f"{template_version}.yaml"
    if not path.is_file():
        raise FileNotFoundError(
            f"Prompt template not found: {path}. "
            "Ensure prompts/{job_id}/{template_version}.yaml exists."
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"Prompt template {path} must be a YAML mapping.")
    for required in ("system_prompt", "user_template"):
        if required not in data:
            raise ValueError(
                f"Prompt template {path} is missing required key '{required}'."
            )
    return data  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# LLM response model — intermediate structure returned by the model
# ---------------------------------------------------------------------------

class _RawNodeType(BaseModel):
    """Raw node type dict as expected from the LLM response."""

    node_class: str
    type: str
    primary_property: str
    qualifier: str | None = None
    additional_properties: list[str] = []


class _RawEdgeType(BaseModel):
    """Raw edge type dict as expected from the LLM response."""

    start_node_type: str
    end_node_type: str
    type: str
    primary_property: str | None = None
    qualifier: str | None = None
    additional_properties: list[str] = []


class SchemaProposal(BaseModel):
    """Intermediate LLM output validated before constructing SchemaVersion.

    The LLM returns a flat JSON object with two lists.  This model validates
    structural correctness before any domain model is constructed.
    """

    nodes: list[_RawNodeType]
    edges: list[_RawEdgeType]


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class SchemaLockedError(Exception):
    """Raised when an operation attempts to modify an already-locked schema.

    Attributes:
        run_id:       The run whose schema is locked.
        version_hash: The hash of the existing locked schema.
    """

    def __init__(self, run_id: str, version_hash: str) -> None:
        super().__init__(
            f"Schema for run '{run_id}' is locked (version_hash={version_hash}). "
            "No modifications are permitted after approval."
        )
        self.run_id = run_id
        self.version_hash = version_hash


class LLMProposalError(Exception):
    """Raised when the LLM returns an invalid or empty schema proposal.

    The service fails closed rather than silently accepting a partial result.
    """


# ---------------------------------------------------------------------------
# Schema service
# ---------------------------------------------------------------------------

class SchemaService:
    """Lifecycle manager for the domain schema of a single run.

    The service is stateless — all state lives in Redis.  It is safe to
    instantiate multiple instances (e.g. per-request) without data races
    because Redis SET operations are atomic and the cache layer is the sole
    persistence mechanism for schema lock state.

    Args:
        llm_client:   LLMClient instance (injectable for testing).
        job_config:   JobConfig for the schema proposal LLM call.
    """

    _JOB_ID: str = "schema_propose"
    _TEMPLATE_VERSION: str = "v1"

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        job_config: JobConfig | None = None,
    ) -> None:
        self._llm = llm_client or LLMClient()
        self._job_config = job_config or JobConfig(
            job_id=self._JOB_ID,
            model="openrouter/hunter-alpha",
            temperature=0.2,
            response_format={"type": "json_object"},
        )

    # ------------------------------------------------------------------
    # propose
    # ------------------------------------------------------------------

    async def propose(
        self,
        run_id: str,
        domain_description: str,
        model_override: str | None = None,
    ) -> SchemaProposal:
        """Generate a candidate schema by calling the LLM.

        Loads the versioned prompt template, formats the user message, calls
        the LLM, and validates the structured response.

        Fails closed if:
          - The schema for this run is already locked (SchemaLockedError).
          - The LLM returns None / fails validation (LLMProposalError).

        Args:
            run_id:             Governed run identifier.
            domain_description: Free-text description of the user's domain.
            model_override:     Optional OpenRouter model ID.  When provided,
                                overrides the default model for this call only.

        Returns:
            A validated SchemaProposal (not yet a locked SchemaVersion).

        Raises:
            SchemaLockedError:  Schema already approved for this run.
            LLMProposalError:   LLM call failed or returned invalid output.
            FileNotFoundError:  Prompt template file not found.
        """
        await self._assert_not_locked(run_id)

        template = _load_prompt_template(self._JOB_ID, self._TEMPLATE_VERSION)
        system_prompt: str = template["system_prompt"]
        user_message: str = template["user_template"].format(
            domain_description=domain_description,
        )

        # Apply model override if provided.
        job_config = self._job_config
        if model_override and model_override.strip():
            job_config = JobConfig(
                job_id=self._job_config.job_id,
                model=model_override.strip(),
                temperature=self._job_config.temperature,
                response_format=self._job_config.response_format,
            )

        logger.info(
            "schema_propose_start",
            run_id=run_id,
            job_id=self._JOB_ID,
            model=job_config.model,
            domain_length=len(domain_description),
        )

        result: SchemaProposal | None = await self._llm.call(
            job=job_config,
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=SchemaProposal,
            run_id=run_id,
        )

        if result is None:
            logger.error(
                "schema_propose_llm_failed",
                run_id=run_id,
                job_id=self._JOB_ID,
            )
            raise LLMProposalError(
                f"LLM returned no valid schema proposal for run '{run_id}'. "
                "Check llm_call_* log events for details."
            )

        if not result.nodes:
            logger.error(
                "schema_propose_empty_nodes",
                run_id=run_id,
                edge_count=len(result.edges),
            )
            raise LLMProposalError(
                f"LLM proposal for run '{run_id}' contains no node types. "
                "A schema must define at least one node type."
            )

        logger.info(
            "schema_propose_complete",
            run_id=run_id,
            node_count=len(result.nodes),
            edge_count=len(result.edges),
        )
        return result

    # ------------------------------------------------------------------
    # approve
    # ------------------------------------------------------------------

    async def approve(
        self,
        run_id: str,
        schema_data: SchemaProposal,
    ) -> SchemaVersion:
        """Validate, freeze, and cache the approved schema for a run.

        The caller supplies a SchemaProposal (which may have been edited by
        the user after the initial proposal).  This method:
          1. Rejects the call if the schema is already locked.
          2. Constructs typed NodeTypeDef / EdgeTypeDef objects (fail-closed
             on any Pydantic validation error).
          3. Wraps them in a frozen SchemaVersion.
          4. Writes to cache with no TTL (immutable for run lifetime).
          5. Emits a structured log event.

        Args:
            run_id:      Governed run identifier.
            schema_data: Caller-supplied proposal (possibly user-edited).

        Returns:
            The frozen, cached SchemaVersion.

        Raises:
            SchemaLockedError:   Schema already approved for this run.
            ValidationError:     A node/edge definition fails Pydantic validation.
        """
        await self._assert_not_locked(run_id)

        # Promote raw types to frozen domain models.  ValidationError
        # propagates to the caller (fail-closed — CLAUDE.md §4.4).
        try:
            nodes: tuple[NodeTypeDef, ...] = tuple(
                NodeTypeDef(
                    node_class=n.node_class,
                    type=n.type,
                    primary_property=n.primary_property,
                    qualifier=n.qualifier,
                    additional_properties=tuple(n.additional_properties),
                )
                for n in schema_data.nodes
            )
            edges: tuple[EdgeTypeDef, ...] = tuple(
                EdgeTypeDef(
                    start_node_type=e.start_node_type,
                    end_node_type=e.end_node_type,
                    type=e.type,
                    primary_property=e.primary_property,
                    qualifier=e.qualifier,
                    additional_properties=tuple(e.additional_properties),
                )
                for e in schema_data.edges
            )
        except ValidationError:
            logger.error(
                "schema_approve_validation_failed",
                run_id=run_id,
                node_count=len(schema_data.nodes),
                edge_count=len(schema_data.edges),
            )
            raise

        schema_version = SchemaVersion(run_id=run_id, nodes=nodes, edges=edges)

        # Cache with no TTL — immutable after lock (SKILL-D R-D8).
        cache = get_cache_client()
        key = CacheKey.schema(run_id=run_id)
        await cache.set(key, schema_version, ttl=None)

        logger.info(
            "schema_locked",
            run_id=run_id,
            version_hash=schema_version.version_hash,
            node_count=len(nodes),
            edge_count=len(edges),
        )
        return schema_version

    # ------------------------------------------------------------------
    # get_current
    # ------------------------------------------------------------------

    async def get_current(self, run_id: str) -> SchemaVersion | None:
        """Return the locked SchemaVersion for a run, or None if not yet approved.

        Cache-first: checks Redis before any other source.  A cache miss is
        authoritative — if the schema is not in cache it has not been approved.
        (The service never persists schemas to Neo4j or disk — CLAUDE.md §12.)

        Args:
            run_id: Governed run identifier.

        Returns:
            The frozen SchemaVersion if locked, else None.

        Log events:
            schema_cache_hit    DEBUG — run_id, version_hash
            schema_not_found    DEBUG — run_id
        """
        cache = get_cache_client()
        key = CacheKey.schema(run_id=run_id)
        schema = await cache.get(key, model=SchemaVersion)

        if schema is not None:
            logger.debug(
                "schema_cache_hit",
                run_id=run_id,
                version_hash=schema.version_hash,
            )
            return schema

        logger.debug("schema_not_found", run_id=run_id)
        return None

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------

    async def validate(
        self,
        run_id: str,
        schema_data: SchemaProposal,
    ) -> list[str]:
        """Validate a user-supplied schema without LLM involvement.

        Performs structural and semantic checks beyond Pydantic field
        validation.  Returns a list of human-readable issue strings.
        An empty list means the schema is ready for approval.

        Checks performed:
          1. Schema not already locked for this run.
          2. Node count within 1–8 range.
          3. Edge count within 1–15 range.
          4. Node type uniqueness.
          5. Node class in allowed set.
          6. Node type is PascalCase (starts with uppercase, no underscores).
          7. Edge type is SCREAMING_SNAKE_CASE (uppercase + underscores).
          8. Edge start/end node types reference defined node types.
          9. No orphan node types (every node participates in at least one edge).
         10. Primary property is non-empty snake_case on every node.
         11. Pydantic validation of NodeTypeDef / EdgeTypeDef construction.

        Args:
            run_id:      Governed run identifier.
            schema_data: User-supplied schema to validate.

        Returns:
            List of issue descriptions.  Empty means valid.
        """
        issues: list[str] = []

        # 1. Lock check
        existing = await self.get_current(run_id)
        if existing is not None:
            issues.append(
                f"Schema for run '{run_id}' is already locked "
                f"(version_hash={existing.version_hash})."
            )
            return issues  # no point continuing

        # 2. Node count
        if not schema_data.nodes:
            issues.append("Schema must define at least one node type.")
        elif len(schema_data.nodes) > 8:
            issues.append(
                f"Too many node types ({len(schema_data.nodes)}). Maximum is 8."
            )

        # 3. Edge count
        if not schema_data.edges:
            issues.append("Schema must define at least one edge type.")
        elif len(schema_data.edges) > 15:
            issues.append(
                f"Too many edge types ({len(schema_data.edges)}). Maximum is 15."
            )

        # 4. Node type uniqueness
        node_types: list[str] = [n.type for n in schema_data.nodes]
        seen: set[str] = set()
        for nt in node_types:
            if nt in seen:
                issues.append(f"Duplicate node type: '{nt}'.")
            seen.add(nt)
        node_type_set = set(node_types)

        # 5. Allowed node classes
        allowed_classes = {
            "Entity", "Event", "Concept", "Document",
            "Location", "Asset", "Metric", "Role",
        }
        for n in schema_data.nodes:
            if n.node_class not in allowed_classes:
                issues.append(
                    f"Node '{n.type}' has invalid node_class '{n.node_class}'. "
                    f"Allowed: {', '.join(sorted(allowed_classes))}."
                )

        # 6. Node type PascalCase check
        for n in schema_data.nodes:
            t = n.type
            if not t or not t[0].isupper() or "_" in t:
                issues.append(
                    f"Node type '{t}' must be PascalCase "
                    "(start with uppercase, no underscores)."
                )

        # 7. Edge type SCREAMING_SNAKE_CASE check
        for e in schema_data.edges:
            t = e.type
            if t != t.upper() or not t.replace("_", "").isalpha():
                issues.append(
                    f"Edge type '{t}' must be SCREAMING_SNAKE_CASE "
                    "(uppercase letters and underscores only)."
                )

        # 8. Edge references valid node types
        for e in schema_data.edges:
            if e.start_node_type not in node_type_set:
                issues.append(
                    f"Edge '{e.type}' references undefined start_node_type "
                    f"'{e.start_node_type}'."
                )
            if e.end_node_type not in node_type_set:
                issues.append(
                    f"Edge '{e.type}' references undefined end_node_type "
                    f"'{e.end_node_type}'."
                )

        # 9. Orphan node check
        referenced_types: set[str] = set()
        for e in schema_data.edges:
            referenced_types.add(e.start_node_type)
            referenced_types.add(e.end_node_type)
        for nt in node_type_set:
            if nt not in referenced_types:
                issues.append(
                    f"Node type '{nt}' is not referenced by any edge "
                    "(orphan node)."
                )

        # 10. Primary property snake_case check
        for n in schema_data.nodes:
            pp = n.primary_property
            if pp != pp.lower() or " " in pp:
                issues.append(
                    f"Node '{n.type}' primary_property '{pp}' must be "
                    "lowercase snake_case."
                )

        # 11. Pydantic construction check (catches field-level validation)
        if not issues:
            try:
                tuple(
                    NodeTypeDef(
                        node_class=n.node_class,
                        type=n.type,
                        primary_property=n.primary_property,
                        qualifier=n.qualifier,
                        additional_properties=tuple(n.additional_properties),
                    )
                    for n in schema_data.nodes
                )
                tuple(
                    EdgeTypeDef(
                        start_node_type=e.start_node_type,
                        end_node_type=e.end_node_type,
                        type=e.type,
                        primary_property=e.primary_property,
                        qualifier=e.qualifier,
                        additional_properties=tuple(e.additional_properties),
                    )
                    for e in schema_data.edges
                )
            except ValidationError as exc:
                issues.append(f"Field validation error: {exc}")

        logger.info(
            "schema_validate_complete",
            run_id=run_id,
            node_count=len(schema_data.nodes),
            edge_count=len(schema_data.edges),
            issue_count=len(issues),
        )
        return issues

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _assert_not_locked(self, run_id: str) -> None:
        """Raise SchemaLockedError if a locked schema already exists.

        Used as a guard at the start of both propose() and approve().
        """
        existing = await self.get_current(run_id)
        if existing is not None:
            logger.warning(
                "schema_mutation_rejected",
                run_id=run_id,
                version_hash=existing.version_hash,
            )
            raise SchemaLockedError(
                run_id=run_id,
                version_hash=existing.version_hash,
            )


# ---------------------------------------------------------------------------
# Process-level singleton factory
# ---------------------------------------------------------------------------

_service_instance: SchemaService | None = None


def get_schema_service() -> SchemaService:
    """Return the process-level SchemaService singleton.

    Uses a module-level variable rather than lru_cache so that it is possible
    to replace the instance in tests without import-level side effects.
    """
    global _service_instance
    if _service_instance is None:
        _service_instance = SchemaService()
    return _service_instance
