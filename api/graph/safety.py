"""
api/graph/safety.py — Injection guards and Cypher templates for Agent-C.

This module centralises the identifier-validation logic and static/dynamic
Cypher template constants used by the ExecutionAgent (Agent-C) in
``api/agents/execution.py``.  Extracting them here keeps execution.py
focused on orchestration while making the safety surface easy to audit
independently.

**Injection guard contract**:
  All data values flow through Cypher ``$parameters``.  Only node labels,
  relationship types, and property names are embedded in Cypher text —
  and only after passing a strict regex check.  A failed check raises
  ``ExecutionInjectionError`` *before* the query string is assembled.

**Template naming convention**:
  - ``_*`` (no suffix)  — static Cypher (no ``.format()``).
  - ``_*_T``            — dynamic template; call ``.format(...)`` after
    validating every substitution value with ``_vi()`` / ``_vp()``.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Injection guards
# ---------------------------------------------------------------------------

_SAFE_IDENTIFIER_RE: re.Pattern[str] = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SAFE_PROP_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ExecutionInjectionError(ValueError):
    """Raised when a label, rel type, or property name fails identifier validation."""


def _vi(value: str, kind: str) -> str:
    """Validate that value is safe to embed as a Neo4j label or relationship type."""
    if not _SAFE_IDENTIFIER_RE.match(value):
        raise ExecutionInjectionError(
            f"Invalid Neo4j identifier for {kind}: {value!r}. "
            "Must match ^[A-Za-z][A-Za-z0-9_]*$."
        )
    return value


def _vp(name: str) -> str:
    """Validate that name is safe to embed as a property name in SET/REMOVE."""
    if not _SAFE_PROP_RE.match(name):
        raise ExecutionInjectionError(
            f"Invalid property name: {name!r}. Must match ^[A-Za-z_][A-Za-z0-9_]*$."
        )
    return name


# ---------------------------------------------------------------------------
# Cypher templates — module-level constants
#
# Static templates (no .format() call): plain {property: $param} Cypher map syntax.
# Dynamic templates (identifier embedded via .format()): {{...}} for literal
# Cypher braces, {identifier} for the .format() substitution target.
# ---------------------------------------------------------------------------

_NODE_EXISTS = (
    "MATCH (n {_dedupe_key: $dk, _run_id: $run_id}) RETURN count(n) AS c"
)
_EDGE_EXISTS = (
    "MATCH ()-[r]->() WHERE r._dedupe_key = $dk AND r._run_id = $run_id "
    "RETURN count(r) AS c"
)
_UPDATE_NODE = (
    "MATCH (n {_dedupe_key: $dk, _run_id: $run_id}) "
    "SET n += $props, n._last_modified_at = datetime() "
    "RETURN count(n) AS c"
)
_UPDATE_EDGE = (
    "MATCH ()-[r]->() WHERE r._dedupe_key = $dk AND r._run_id = $run_id "
    "SET r += $props, r._last_modified_at = datetime() "
    "RETURN count(r) AS c"
)
_DELETE_NODE = "MATCH (n {_dedupe_key: $dk, _run_id: $run_id}) DETACH DELETE n"
_DELETE_EDGE = (
    "MATCH ()-[r]->() WHERE r._dedupe_key = $dk AND r._run_id = $run_id DELETE r"
)
# For reads during merge: relationship listing from an obsolete node.
_OBS_OUT_RELS = (
    "MATCH (o {_dedupe_key: $ok, _run_id: $run_id})-[r]->(t) "
    "WHERE t._dedupe_key <> $sk "
    "RETURN type(r) AS rt, t._dedupe_key AS tk, properties(r) AS props"
)
_OBS_IN_RELS = (
    "MATCH (t)-[r]->(o {_dedupe_key: $ok, _run_id: $run_id}) "
    "WHERE t._dedupe_key <> $sk "
    "RETURN type(r) AS rt, t._dedupe_key AS tk, properties(r) AS props"
)
# Dynamic templates — call .format(lbl=...) / .format(rt=...) / .format(p=...)
_REMOVE_NODE_PROP = (
    "MATCH (n {{_dedupe_key: $dk, _run_id: $run_id}}) "
    "SET n.{p} = null RETURN count(n) AS c"
)
_REMOVE_EDGE_PROP = (
    "MATCH ()-[r]->() WHERE r._dedupe_key = $dk AND r._run_id = $run_id "
    "SET r.{p} = null RETURN count(r) AS c"
)
_CREATE_NODE_T = (
    "MERGE (n:`{lbl}` {{_dedupe_key: $dk}}) "
    "ON CREATE SET n += $props, n._run_id = $run_id, "
    "              n._schema_version = $sv, n._created_at = datetime() "
    "ON MATCH  SET n._last_seen_at = datetime() "
    "RETURN count(n) AS c"
)
_CREATE_EDGE_T = (
    "MATCH (s {{_dedupe_key: $sdk, _run_id: $run_id}}) "
    "MATCH (e {{_dedupe_key: $edk, _run_id: $run_id}}) "
    "MERGE (s)-[r:`{rt}`]->(e) "
    "ON CREATE SET r += $props, r._dedupe_key = $dk, r._run_id = $run_id, "
    "              r._schema_version = $sv, r._created_at = datetime() "
    "ON MATCH  SET r._last_seen_at = datetime() "
    "RETURN count(r) AS c"
)
_MERGE_OUT_T = (
    "MATCH (s {{_dedupe_key: $sk, _run_id: $run_id}}) "
    "MATCH (e {{_dedupe_key: $tk, _run_id: $run_id}}) "
    "MERGE (s)-[r:`{rt}`]->(e) "
    "ON CREATE SET r += $props, r._run_id = $run_id, r._created_at = datetime() "
    "ON MATCH  SET r._last_seen_at = datetime()"
)
_MERGE_IN_T = (
    "MATCH (s {{_dedupe_key: $tk, _run_id: $run_id}}) "
    "MATCH (e {{_dedupe_key: $sk, _run_id: $run_id}}) "
    "MERGE (s)-[r:`{rt}`]->(e) "
    "ON CREATE SET r += $props, r._run_id = $run_id, r._created_at = datetime() "
    "ON MATCH  SET r._last_seen_at = datetime()"
)
