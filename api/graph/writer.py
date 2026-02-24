"""
api/graph/writer.py — Parameterized graph write operations (SPEC-04 S-04.4).

This module is the SOLE graph write authority for Phase 3 extraction.

Parameterization contract (CLAUDE.md §4.2)
-------------------------------------------
ALL data values flow as Cypher named parameters ($param_name). The ONLY values
embedded in the Cypher string are node labels and relationship types, and only
after _validate_identifier() confirms they match ^[A-Za-z][A-Za-z0-9_]*$. Any
failing value raises CypherInjectionError before the query is assembled.

MERGE dedupe strategy
---------------------
Nodes: MERGE on _dedupe_key = "|".join((node_type, primary_value, schema_version))
Edges: MATCH start + end nodes by their dedupe keys, MERGE relationship by type.
       _dedupe_key stored on the rel for reference; MERGE condition is type + endpoints.

ON CREATE sets all domain props and governance metadata (_run_id, _schema_version,
_chunk_id, _created_at). ON MATCH updates only _last_seen_at and _run_id so that
first-write canonical property values are preserved across reruns.

was_created detection: RETURN (n._last_seen_at IS NULL) — true only on CREATE
because ON MATCH always sets _last_seen_at and ON CREATE never does.

Edge skips: if start or end nodes are absent, MATCH returns zero rows; the edge
is skipped with a WARNING. write_extraction_result() writes all nodes first.

Governance prefix: user-supplied properties whose keys start with "_" are stripped
before each write to prevent collisions with writer-managed metadata fields.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from api.graph.client import Neo4jClient
from api.graph.models import EdgeWriteResult, ExtractionWriteResult, NodeWriteResult
from api.models.extraction import ExtractedEdge, ExtractedNode, ExtractionResult
from api.observability.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Cypher injection guard
# ---------------------------------------------------------------------------

_SAFE_IDENTIFIER_RE: re.Pattern[str] = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class CypherInjectionError(ValueError):
    """Raised when a node label or rel type fails identifier validation.

    Signals a gap in upstream validation — ExtractionService must validate all
    entity types against the locked schema before constructing Extracted* models.
    """


def _validate_identifier(value: str, kind: str) -> str:
    """Validate that value is safe to embed as a Neo4j label or rel type.

    Only ASCII letters, digits, and underscores are allowed; first char must be
    a letter. Backtick quoting in the Cypher template then prevents the validated
    value from escaping the label context.

    Returns value unchanged if valid. Raises CypherInjectionError otherwise.
    """
    if not _SAFE_IDENTIFIER_RE.match(value):
        raise CypherInjectionError(
            f"Invalid Neo4j identifier for {kind}: {value!r}. "
            f"Must match ^[A-Za-z][A-Za-z0-9_]*$. "
            f"ExtractionService must validate entity types against the locked schema."
        )
    return value


# ---------------------------------------------------------------------------
# Dedupe key serialization
# ---------------------------------------------------------------------------


def _serialize_node_dedupe_key(
    node_type: str, primary_value: str, schema_version: str
) -> str:
    """Canonical string for a node dedupe key: "|".join((type, value, version)).

    Must produce the same string as "|".join(node.node_dedupe_key) for an
    equivalent ExtractedNode — edge writes rely on this identity to MATCH
    endpoint nodes by the key written during the node write.
    """
    return "|".join((node_type, primary_value, schema_version))


def _serialize_rel_dedupe_key(rel_dedupe_key: tuple[str, str, str, str]) -> str:
    """Canonical string for a rel dedupe key: "::".join(rel_dedupe_key tuple).

    Uses "::" as the outer separator because the inner start_key / end_key
    components already contain "|" separators (node dedupe key format).
    Stored as r._dedupe_key on the relationship for reference only.
    """
    return "::".join(rel_dedupe_key)


# ---------------------------------------------------------------------------
# Cypher templates
# ---------------------------------------------------------------------------
# {label}, {start_label}, {end_label}, {rel_type} are the ONLY .format() vars.
# All other curly-brace pairs are escaped as {{...}} → literal Cypher map syntax.
# $param_name tokens are Cypher parameters, left intact by .format().

_MERGE_NODE_TEMPLATE: str = (
    "MERGE (n:`{label}` {{_dedupe_key: $dedupe_key}})\n"
    "ON CREATE SET n += $domain_props,\n"
    "              n._primary_value = $primary_value,\n"
    "              n._run_id = $run_id,\n"
    "              n._schema_version = $schema_version,\n"
    "              n._chunk_id = $chunk_id,\n"
    "              n._created_at = datetime()\n"
    "ON MATCH SET  n._last_seen_at = datetime(),\n"
    "              n._run_id = $run_id\n"
    "RETURN n._dedupe_key AS dedupe_key,\n"
    "       (n._last_seen_at IS NULL) AS was_created"
)

_MERGE_EDGE_TEMPLATE: str = (
    "MATCH (s:`{start_label}` {{_dedupe_key: $start_dedupe_key}})\n"
    "MATCH (e:`{end_label}` {{_dedupe_key: $end_dedupe_key}})\n"
    "MERGE (s)-[r:`{rel_type}`]->(e)\n"
    "ON CREATE SET r += $domain_props,\n"
    "              r._dedupe_key = $rel_dedupe_key,\n"
    "              r._run_id = $run_id,\n"
    "              r._schema_version = $schema_version,\n"
    "              r._chunk_id = $chunk_id,\n"
    "              r._created_at = datetime()\n"
    "ON MATCH SET  r._last_seen_at = datetime(),\n"
    "              r._run_id = $run_id\n"
    "RETURN r._dedupe_key AS dedupe_key,\n"
    "       (r._last_seen_at IS NULL) AS was_created"
)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class GraphWriter:
    """Write ExtractedNode / ExtractedEdge models to Neo4j via MERGE.

    The SOLE graph write authority for Phase 3 extraction (CLAUDE.md §4.2).
    No other extraction-path module may call Neo4j directly.

    Each write method runs one transaction. Partial failures are isolated —
    a failing node write does not abort subsequent entity writes.

    Args:
        client: An open Neo4jClient (get_neo4j_client() from api.graph.client).
                open() must have been called before any write method is invoked.
    """

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Node write
    # ------------------------------------------------------------------

    async def write_node(self, node: ExtractedNode) -> NodeWriteResult:
        """MERGE a single extracted node into Neo4j.

        On CREATE: domain properties and all governance metadata are set.
        On MATCH:  only _last_seen_at and _run_id are updated; domain
                   properties are preserved from the first write.

        Raises:
            CypherInjectionError: If node.node_type fails identifier validation.
            neo4j.exceptions.Neo4jError: On unrecoverable Neo4j errors.

        Log events:
            node_write  INFO — run_id, node_type, dedupe_key, action
        """
        label = _validate_identifier(node.node_type, "node_type")
        cypher = _MERGE_NODE_TEMPLATE.format(label=label)
        dedupe_key = _serialize_node_dedupe_key(
            node.node_type, node.primary_value, node.schema_version
        )
        domain_props: dict[str, Any] = {
            k: v for k, v in node.properties.items() if not k.startswith("_")
        }
        params: dict[str, Any] = {
            "dedupe_key": dedupe_key,
            "domain_props": domain_props,
            "primary_value": node.primary_value,
            "run_id": node.run_id,
            "schema_version": node.schema_version,
            "chunk_id": node.chunk_id,
        }

        async def _tx(tx: Any) -> dict[str, Any] | None:
            result = await tx.run(cypher, **params)
            record = await result.single()
            return dict(record) if record else None

        record = await self._client.run_write_transaction(_tx)

        was_created: bool = bool(record.get("was_created")) if record else False
        action: Literal["merge_created", "merge_matched"] = (
            "merge_created" if was_created else "merge_matched"
        )
        logger.info(
            "node_write",
            run_id=node.run_id,
            node_type=node.node_type,
            dedupe_key=dedupe_key,
            action=action,
        )
        return NodeWriteResult(
            run_id=node.run_id,
            chunk_id=node.chunk_id,
            node_type=node.node_type,
            dedupe_key=dedupe_key,
            action=action,
        )

    # ------------------------------------------------------------------
    # Edge write
    # ------------------------------------------------------------------

    async def write_edge(self, edge: ExtractedEdge) -> EdgeWriteResult:
        """MERGE a single extracted relationship into Neo4j.

        MATCHes start and end nodes by their dedupe keys, then MERGEs the
        relationship by type. If either endpoint is absent, the query returns
        no rows and the edge result carries action="skipped".

        Raises:
            CypherInjectionError: If any identifier fails validation.
            neo4j.exceptions.Neo4jError: On unrecoverable Neo4j errors.

        Log events:
            edge_write              INFO    — run_id, rel_type, dedupe_key, action
            edge_endpoint_missing   WARNING — run_id, rel_type, start/end types
        """
        start_label = _validate_identifier(edge.start_node_type, "start_node_type")
        end_label = _validate_identifier(edge.end_node_type, "end_node_type")
        rel_type_validated = _validate_identifier(edge.rel_type, "rel_type")

        cypher = _MERGE_EDGE_TEMPLATE.format(
            start_label=start_label,
            end_label=end_label,
            rel_type=rel_type_validated,
        )
        start_dedupe_key = _serialize_node_dedupe_key(
            edge.start_node_type, edge.start_primary_value, edge.schema_version
        )
        end_dedupe_key = _serialize_node_dedupe_key(
            edge.end_node_type, edge.end_primary_value, edge.schema_version
        )
        rel_dedupe_key_str = _serialize_rel_dedupe_key(edge.rel_dedupe_key)
        domain_props: dict[str, Any] = {
            k: v for k, v in edge.properties.items() if not k.startswith("_")
        }
        params: dict[str, Any] = {
            "start_dedupe_key": start_dedupe_key,
            "end_dedupe_key": end_dedupe_key,
            "rel_dedupe_key": rel_dedupe_key_str,
            "domain_props": domain_props,
            "run_id": edge.run_id,
            "schema_version": edge.schema_version,
            "chunk_id": edge.chunk_id,
        }

        async def _tx(tx: Any) -> dict[str, Any] | None:
            result = await tx.run(cypher, **params)
            record = await result.single()
            return dict(record) if record else None

        record = await self._client.run_write_transaction(_tx)

        if record is None:
            logger.warning(
                "edge_endpoint_missing",
                run_id=edge.run_id,
                rel_type=edge.rel_type,
                start_node_type=edge.start_node_type,
                end_node_type=edge.end_node_type,
            )
            return EdgeWriteResult(
                run_id=edge.run_id,
                chunk_id=edge.chunk_id,
                rel_type=edge.rel_type,
                dedupe_key=rel_dedupe_key_str,
                action="skipped",
            )

        was_created = bool(record.get("was_created"))
        action: Literal["merge_created", "merge_matched", "skipped"] = (
            "merge_created" if was_created else "merge_matched"
        )
        logger.info(
            "edge_write",
            run_id=edge.run_id,
            rel_type=edge.rel_type,
            dedupe_key=rel_dedupe_key_str,
            action=action,
        )
        return EdgeWriteResult(
            run_id=edge.run_id,
            chunk_id=edge.chunk_id,
            rel_type=edge.rel_type,
            dedupe_key=rel_dedupe_key_str,
            action=action,
        )

    # ------------------------------------------------------------------
    # Full extraction result
    # ------------------------------------------------------------------

    async def write_extraction_result(
        self, result: ExtractionResult
    ) -> ExtractionWriteResult:
        """Write all nodes then all edges from one ExtractionResult.

        Nodes are written first so that edge endpoint MATCHes succeed. Each
        entity runs its own transaction — a single failure does not abort the
        remaining writes. An empty ExtractionResult (no nodes, no edges) is
        valid and produces all-zero counts.

        Log events:
            extraction_write_complete  INFO — run_id, chunk_id, all counts
        """
        node_results: list[NodeWriteResult] = []
        for node in result.nodes:
            node_results.append(await self.write_node(node))

        edge_results: list[EdgeWriteResult] = []
        for edge in result.edges:
            edge_results.append(await self.write_edge(edge))

        nodes_created = sum(1 for n in node_results if n.action == "merge_created")
        nodes_matched = sum(1 for n in node_results if n.action == "merge_matched")
        edges_created = sum(1 for e in edge_results if e.action == "merge_created")
        edges_matched = sum(1 for e in edge_results if e.action == "merge_matched")
        edges_skipped = sum(1 for e in edge_results if e.action == "skipped")

        logger.info(
            "extraction_write_complete",
            run_id=result.run_id,
            chunk_id=result.chunk_id,
            nodes_created=nodes_created,
            nodes_matched=nodes_matched,
            edges_created=edges_created,
            edges_matched=edges_matched,
            edges_skipped=edges_skipped,
        )
        return ExtractionWriteResult(
            run_id=result.run_id,
            chunk_id=result.chunk_id,
            schema_version=result.schema_version,
            nodes_created=nodes_created,
            nodes_matched=nodes_matched,
            edges_created=edges_created,
            edges_matched=edges_matched,
            edges_skipped=edges_skipped,
            node_results=tuple(node_results),
            edge_results=tuple(edge_results),
        )
