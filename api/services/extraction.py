"""
api/services/extraction.py — AI-assisted extraction service (SPEC-04 S-04.2).

Orchestrates Phase 3: chunk text + locked schema → LLM → validated ExtractionResult.
Does NOT write to Neo4j; graph writes are handled by api/graph/writer.py.

Pipeline (extract_chunk):
  1. Retrieve locked SchemaVersion from cache (CacheKey.schema(run_id)).
     Fail closed on cache miss — schema must be approved before extraction.
  2. Load versioned prompt template from prompts/extraction/v1.yaml.
  3. Call LLM via LLMClient; validate response against _LLMExtractionOutput.
  4. Assert every extracted node_type / rel_type exists in the locked schema
     (deterministic, non-LLM check — CLAUDE.md §4.3).
  5. Promote validated output to frozen ExtractionResult, stamping run_id,
     chunk_id, and schema_version onto every entity.

Fail-closed contract: ExtractionError (a ValueError subclass) is raised on:
  schema not in cache | LLM returns None | type not in schema | promotion fails

Cache (SKILL-D R-D8): locked schema — CacheKey.schema(run_id), no TTL.
Prompts (CLAUDE.md §14): template loaded from prompts/, no inline strings.
Sensitive data (SKILL-D R-D5): chunk_text passed to LLM, never logged.

Prompt loading uses the shared loader from api/common/prompts.py (SKILL-B R-B7).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, field_validator

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.common.prompts import load_prompt_template
from api.models.extraction import ExtractedEdge, ExtractedNode, ExtractionResult
from api.observability.logger import get_logger
from api.schema.models import SchemaVersion
from api.services.llm import JobConfig, LLMClient

logger = get_logger(__name__)


def _schema_to_prompt_json(schema: SchemaVersion) -> str:
    """Serialize the locked schema to compact JSON for LLM prompts.

    Includes only extraction-relevant fields: type names, primary_property,
    additional_properties. Excludes version_hash, run_id, qualifiers.
    Never logged (log version_hash instead — SKILL-D R-D5).
    """
    payload: dict[str, Any] = {
        "nodes": [
            {
                "type": n.type,
                "primary_property": n.primary_property,
                "additional_properties": list(n.additional_properties),
            }
            for n in schema.nodes
        ],
        "edges": [
            {
                "type": e.type,
                "start_node_type": e.start_node_type,
                "end_node_type": e.end_node_type,
                "primary_property": e.primary_property,
                "additional_properties": list(e.additional_properties),
            }
            for e in schema.edges
        ],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, indent=2)


# ---------------------------------------------------------------------------
# Private LLM-facing intermediate models
# ---------------------------------------------------------------------------

def _sanitize_unicode(v: str) -> str:
    """Replace non-ASCII whitespace (e.g. \\xa0) with regular space, then trim."""
    import unicodedata

    cleaned = "".join(
        " " if (unicodedata.category(ch).startswith("Z") and ch != " ") else ch
        for ch in v
    )
    return " ".join(cleaned.split())


class _RawNode(BaseModel):
    """Raw node dict as returned by the LLM (not yet a governed domain model)."""

    node_type: str
    primary_value: str
    properties: dict[str, Any] = {}

    @field_validator("node_type", "primary_value")
    @classmethod
    def _clean(cls, v: str) -> str:
        return _sanitize_unicode(v)


class _RawEdge(BaseModel):
    """Raw edge dict as returned by the LLM (not yet a governed domain model)."""

    rel_type: str
    start_node_type: str
    start_primary_value: str
    end_node_type: str
    end_primary_value: str
    properties: dict[str, Any] = {}

    @field_validator(
        "rel_type",
        "start_node_type",
        "start_primary_value",
        "end_node_type",
        "end_primary_value",
    )
    @classmethod
    def _clean(cls, v: str) -> str:
        return _sanitize_unicode(v)


class _LLMExtractionOutput(BaseModel):
    """Intermediate LLM output validated before promotion to governed models.

    Empty lists are valid — not every chunk yields extractable entities.
    """

    nodes: list[_RawNode] = []
    edges: list[_RawEdge] = []


class _BatchChunkResult(BaseModel):
    """Per-chunk result within a batch extraction response.

    chunk_id ties the extraction back to the source chunk so results
    can be matched to the correct ExtractionResult after the LLM call.
    """

    chunk_id: str
    nodes: list[_RawNode] = []
    edges: list[_RawEdge] = []


class _BatchExtractionOutput(BaseModel):
    """Top-level batch extraction response wrapper.

    Contains one _BatchChunkResult per input chunk.  The LLM must return
    every chunk_id that was provided in the prompt.
    """

    results: list[_BatchChunkResult]


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class ExtractionError(ValueError):
    """Raised on any extraction failure (schema missing, LLM failure,
    schema conformance violation, promotion failure). Subclasses ValueError
    per the fail-closed contract in CLAUDE.md §4.4."""


# ---------------------------------------------------------------------------
# Extraction service
# ---------------------------------------------------------------------------

class ExtractionService:
    """Orchestrates LLM-assisted entity extraction for a single chunk.

    Stateless — all state lives in Redis. Safe to instantiate multiple times.

    Args:
        llm_client:  LLMClient instance (injectable for testing).
        job_config:  JobConfig for the extraction LLM call.
    """

    _JOB_ID: str = "extraction"
    _TEMPLATE_VERSION: str = "v1"
    _BATCH_TEMPLATE_VERSION: str = "v2"

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        job_config: JobConfig | None = None,
    ) -> None:
        self._llm = llm_client or LLMClient()
        self._job_config = job_config or JobConfig(
            job_id=self._JOB_ID,
            model="openrouter/hunter-alpha",
            temperature=0.2,  # structured extraction
            response_format={"type": "json_object"},
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def extract_chunk(
        self,
        run_id: str,
        chunk_id: str,
        chunk_text: str,
        model_override: str | None = None,
    ) -> ExtractionResult:
        """Extract entities and relationships from a single chunk.

        Args:
            run_id:     Governed run identifier; used to look up the locked schema
                        and stamp provenance on every entity.
            chunk_id:   Source chunk identifier (Chunk.chunk_id); stamped on every
                        ExtractedNode and ExtractedEdge.
            chunk_text: Raw chunk text; passed to the LLM but never logged
                        (SKILL-D R-D5).

        Returns:
            Frozen ExtractionResult. nodes/edges may be empty.

        Raises:
            ExtractionError: Schema missing | LLM failure | type mismatch |
                             promotion failure.

        Log events:
            extraction_start             INFO  — run_id, chunk_id
            extraction_schema_cache_hit  DEBUG — run_id, version_hash
            extraction_schema_not_found  ERROR — run_id, chunk_id
            extraction_llm_start         DEBUG — run_id, chunk_id, model
            extraction_llm_failed        ERROR — run_id, chunk_id
            extraction_node_type_mismatch ERROR — run_id, chunk_id, invalid_type
            extraction_edge_type_mismatch ERROR — run_id, chunk_id, invalid_type
            extraction_promote_failed    ERROR — run_id, chunk_id, error
            extraction_complete          INFO  — run_id, chunk_id, node_count, edge_count
        """
        logger.info("extraction_start", run_id=run_id, chunk_id=chunk_id)

        schema = await self._get_schema(run_id=run_id, chunk_id=chunk_id)
        template = load_prompt_template(self._JOB_ID, self._TEMPLATE_VERSION)
        system_prompt: str = template["system_prompt"]
        user_message: str = template["user_template"].format(
            schema_json=_schema_to_prompt_json(schema),
            chunk_text=chunk_text,
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

        logger.debug(
            "extraction_llm_start",
            run_id=run_id,
            chunk_id=chunk_id,
            job_id=self._JOB_ID,
            model=job_config.model,
        )
        llm_output: _LLMExtractionOutput | None = await self._llm.call(
            job=job_config,
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=_LLMExtractionOutput,
            run_id=run_id,
        )

        if llm_output is None:
            logger.error("extraction_llm_failed", run_id=run_id, chunk_id=chunk_id)
            raise ExtractionError(
                f"LLM returned invalid output for chunk '{chunk_id}' in run '{run_id}'. "
                "Check llm_call_* log events for details."
            )

        self._validate_schema_conformance(llm_output, schema, run_id, chunk_id)
        result = self._promote_to_result(llm_output, run_id, chunk_id, schema)

        logger.info(
            "extraction_complete",
            run_id=run_id,
            chunk_id=chunk_id,
            node_count=len(result.nodes),
            edge_count=len(result.edges),
            schema_version=schema.version_hash,
        )
        return result

    async def extract_batch(
        self,
        run_id: str,
        chunks: list[tuple[str, str]],
        model_override: str | None = None,
    ) -> dict[str, ExtractionResult]:
        """Extract entities and relationships from multiple chunks in one LLM call.

        Args:
            run_id:     Governed run identifier.
            chunks:     List of (chunk_id, chunk_text) tuples.  Order is preserved
                        in the prompt but does not affect the keyed results.
            model_override: Optional OpenRouter model override.

        Returns:
            Dict mapping chunk_id → ExtractionResult.  Chunks that fail
            schema validation or promotion are omitted (logged at ERROR).
            Empty dict if the LLM call itself fails.

        Raises:
            ExtractionError: Schema not found in cache.

        Log events:
            extraction_batch_start        INFO  — run_id, batch_size
            extraction_batch_llm_failed   ERROR — run_id, batch_size
            extraction_batch_chunk_failed ERROR — run_id, chunk_id, error
            extraction_batch_complete     INFO  — run_id, succeeded, failed
        """
        batch_size = len(chunks)
        logger.info("extraction_batch_start", run_id=run_id, batch_size=batch_size)

        # Use first chunk_id for schema lookup logging (schema is run-scoped).
        schema = await self._get_schema(run_id=run_id, chunk_id=chunks[0][0])
        schema_json = _schema_to_prompt_json(schema)

        # Build the chunks block: each chunk as a numbered section.
        chunk_sections: list[str] = []
        for chunk_id, chunk_text in chunks:
            chunk_sections.append(
                f"### Chunk: {chunk_id}\n{chunk_text}"
            )
        chunks_block = "\n\n---\n\n".join(chunk_sections)

        # Load batch prompt template (v2).
        template = load_prompt_template(self._JOB_ID, self._BATCH_TEMPLATE_VERSION)
        system_prompt: str = template["system_prompt"]
        user_message: str = template["user_template"].format(
            schema_json=schema_json,
            batch_size=batch_size,
            chunks_block=chunks_block,
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

        logger.debug(
            "extraction_batch_llm_start",
            run_id=run_id,
            batch_size=batch_size,
            model=job_config.model,
        )

        llm_output: _BatchExtractionOutput | None = await self._llm.call(
            job=job_config,
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=_BatchExtractionOutput,
            run_id=run_id,
        )

        if llm_output is None:
            logger.error(
                "extraction_batch_llm_failed",
                run_id=run_id,
                batch_size=batch_size,
            )
            return {}

        # Index results by chunk_id for lookup.
        result_map: dict[str, _BatchChunkResult] = {
            r.chunk_id: r for r in llm_output.results
        }

        # Validate and promote each chunk's results independently.
        valid_node_types = {n.type for n in schema.nodes}
        valid_edge_types = {e.type for e in schema.edges}
        results: dict[str, ExtractionResult] = {}
        failed_count = 0

        for chunk_id, _ in chunks:
            chunk_result = result_map.get(chunk_id)
            if chunk_result is None:
                logger.error(
                    "extraction_batch_chunk_missing",
                    run_id=run_id,
                    chunk_id=chunk_id,
                )
                failed_count += 1
                continue

            # Wrap in _LLMExtractionOutput for reuse of existing validation.
            single_output = _LLMExtractionOutput(
                nodes=chunk_result.nodes,
                edges=chunk_result.edges,
            )

            try:
                self._validate_schema_conformance(
                    single_output, schema, run_id, chunk_id
                )
                result = self._promote_to_result(
                    single_output, run_id, chunk_id, schema
                )
                results[chunk_id] = result
            except ExtractionError as exc:
                logger.error(
                    "extraction_batch_chunk_failed",
                    run_id=run_id,
                    chunk_id=chunk_id,
                    error=str(exc),
                )
                failed_count += 1

        logger.info(
            "extraction_batch_complete",
            run_id=run_id,
            succeeded=len(results),
            failed=failed_count,
            schema_version=schema.version_hash,
        )
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_schema(self, run_id: str, chunk_id: str) -> SchemaVersion:
        """Return the locked schema from cache; fail closed on miss.

        The schema service stores schemas in Redis only (no secondary store),
        so a cache miss means the schema has not been approved.

        Raises:
            ExtractionError: No locked schema found for run_id.
        """
        cache = get_cache_client()
        key = CacheKey.schema(run_id=run_id)
        schema: SchemaVersion | None = await cache.get(key, model=SchemaVersion)

        if schema is None:
            logger.error(
                "extraction_schema_not_found",
                run_id=run_id,
                chunk_id=chunk_id,
            )
            raise ExtractionError(
                f"No locked schema found for run '{run_id}'. "
                "Approve the domain schema (Phase 1) before running extraction."
            )

        logger.debug(
            "extraction_schema_cache_hit",
            run_id=run_id,
            version_hash=schema.version_hash,
        )
        return schema

    def _validate_schema_conformance(
        self,
        output: _LLMExtractionOutput,
        schema: SchemaVersion,
        run_id: str,
        chunk_id: str,
    ) -> None:
        """Assert every extracted node_type and rel_type exists in the locked schema.

        Deterministic, non-LLM check. Fails closed on the first mismatch.

        Raises:
            ExtractionError: A node_type or rel_type is absent from the schema.
        """
        valid_node_types = {n.type for n in schema.nodes}
        valid_edge_types = {e.type for e in schema.edges}

        for raw_node in output.nodes:
            if raw_node.node_type not in valid_node_types:
                logger.error(
                    "extraction_node_type_mismatch",
                    run_id=run_id,
                    chunk_id=chunk_id,
                    invalid_type=raw_node.node_type,
                    valid_types=sorted(valid_node_types),
                )
                raise ExtractionError(
                    f"Extracted node_type '{raw_node.node_type}' is not in the "
                    f"locked schema for run '{run_id}'. "
                    f"Valid types: {sorted(valid_node_types)}"
                )

        for raw_edge in output.edges:
            if raw_edge.rel_type not in valid_edge_types:
                logger.error(
                    "extraction_edge_type_mismatch",
                    run_id=run_id,
                    chunk_id=chunk_id,
                    invalid_type=raw_edge.rel_type,
                    valid_types=sorted(valid_edge_types),
                )
                raise ExtractionError(
                    f"Extracted rel_type '{raw_edge.rel_type}' is not in the "
                    f"locked schema for run '{run_id}'. "
                    f"Valid types: {sorted(valid_edge_types)}"
                )

    def _promote_to_result(
        self,
        output: _LLMExtractionOutput,
        run_id: str,
        chunk_id: str,
        schema: SchemaVersion,
    ) -> ExtractionResult:
        """Promote validated LLM output to a frozen ExtractionResult.

        Stamps run_id, chunk_id, schema_version onto every node and edge.
        Wraps any Pydantic validation error in ExtractionError (fail-closed).

        Raises:
            ExtractionError: Field-level validation fails during promotion.
        """
        schema_version = schema.version_hash
        try:
            nodes = tuple(
                ExtractedNode(
                    run_id=run_id,
                    chunk_id=chunk_id,
                    schema_version=schema_version,
                    node_type=n.node_type,
                    primary_value=n.primary_value,
                    properties=dict(n.properties),
                )
                for n in output.nodes
            )
            edges = tuple(
                ExtractedEdge(
                    run_id=run_id,
                    chunk_id=chunk_id,
                    schema_version=schema_version,
                    rel_type=e.rel_type,
                    start_node_type=e.start_node_type,
                    start_primary_value=e.start_primary_value,
                    end_node_type=e.end_node_type,
                    end_primary_value=e.end_primary_value,
                    properties=dict(e.properties),
                )
                for e in output.edges
            )
        except Exception as exc:
            logger.error(
                "extraction_promote_failed",
                run_id=run_id,
                chunk_id=chunk_id,
                error=str(exc),
            )
            raise ExtractionError(
                f"Failed to promote LLM output to governed models for "
                f"chunk '{chunk_id}' in run '{run_id}': {exc}"
            ) from exc

        return ExtractionResult(
            run_id=run_id,
            chunk_id=chunk_id,
            schema_version=schema_version,
            nodes=nodes,
            edges=edges,
        )


# ---------------------------------------------------------------------------
# Process-level singleton factory
# ---------------------------------------------------------------------------

_service_instance: ExtractionService | None = None


def get_extraction_service() -> ExtractionService:
    """Return the process-level ExtractionService singleton.

    Module-level variable (not lru_cache) so the instance can be replaced
    in tests without import-level side effects. Mirrors get_schema_service().
    """
    global _service_instance
    if _service_instance is None:
        _service_instance = ExtractionService()
    return _service_instance
