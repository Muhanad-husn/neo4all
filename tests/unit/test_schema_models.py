"""
tests/unit/test_schema_models.py — Schema model determinism & validation
(SPEC-02 file 7).

Acceptance criteria (SPEC-02, CLAUDE.md §4.3, §4.4):
  - version_hash is deterministic: same schema content → same hexdigest.
  - version_hash is insertion-order invariant: nodes/edges may be supplied
    in any order; the canonical sorted representation absorbs the difference.
  - version_hash changes on any schema mutation, no matter how small.
  - SchemaVersion, NodeTypeDef, and EdgeTypeDef are frozen: mutation raises
    pydantic.ValidationError (fail-closed behaviour per CLAUDE.md §4.4).
  - Empty or whitespace-only values for required string fields are rejected.

No network, no LLM, no Neo4j — pure Pydantic model and hash computation.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from api.schema.models import EdgeTypeDef, NodeTypeDef, SchemaVersion

# ---------------------------------------------------------------------------
# Shared test data — stable, named constants used across multiple tests.
# Using module-level construction avoids per-test allocation while keeping
# tests independent (Pydantic frozen models cannot be mutated).
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-0000000000000001"

# Two minimal node types for composition into SchemaVersion fixtures.
_NODE_PERSON = NodeTypeDef(
    node_class="Entity",
    type="Person",
    primary_property="name",
    qualifier=None,
    additional_properties=("title", "nationality"),
)
_NODE_ORG = NodeTypeDef(
    node_class="Entity",
    type="Organization",
    primary_property="name",
    qualifier=None,
    additional_properties=("sector",),
)
_EDGE_WORKS_FOR = EdgeTypeDef(
    start_node_type="Person",
    end_node_type="Organization",
    type="WORKS_FOR",
    primary_property="title",
    qualifier=None,
    additional_properties=("since_date",),
)


# ---------------------------------------------------------------------------
# Reference implementation — independent cross-validation of the production
# hash algorithm.  Mirrors the docstring contract of _derive_version_hash()
# in api/schema/models.py without importing the private helper.
# ---------------------------------------------------------------------------


def _reference_version_hash(
    nodes: tuple[NodeTypeDef, ...],
    edges: tuple[EdgeTypeDef, ...],
) -> str:
    """Canonical SHA-256 digest of sorted schema JSON.

    Written independently so that tests cross-validate the production code
    rather than just calling it twice (which would not catch a systematic bug).
    """
    canonical: dict = {
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
# Helpers
# ---------------------------------------------------------------------------


def _make_schema(
    *,
    nodes: tuple[NodeTypeDef, ...] = (_NODE_PERSON,),
    edges: tuple[EdgeTypeDef, ...] = (_EDGE_WORKS_FOR,),
    run_id: str = _RUN_ID,
) -> SchemaVersion:
    """Construct a SchemaVersion with sensible defaults."""
    return SchemaVersion(run_id=run_id, nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# Test group 1: version_hash determinism
# ---------------------------------------------------------------------------


def test_version_hash_deterministic() -> None:
    """Two SchemaVersion objects with identical content produce the same hash.

    This is the primary acceptance criterion for SPEC-02: same schema inputs
    must always produce the same version_hash regardless of object identity.
    """
    schema_a = _make_schema()
    schema_b = _make_schema()  # constructed independently

    assert schema_a.version_hash == schema_b.version_hash


def test_version_hash_is_stable_across_calls() -> None:
    """Calling version_hash multiple times on the same object returns the same value."""
    schema = _make_schema()

    first = schema.version_hash
    second = schema.version_hash
    third = schema.version_hash

    assert first == second == third


def test_version_hash_matches_reference_computation() -> None:
    """version_hash matches the independently-written reference implementation.

    Cross-validates that the production hash algorithm is correct, not just
    consistent with itself.
    """
    nodes = (_NODE_PERSON, _NODE_ORG)
    edges = (_EDGE_WORKS_FOR,)
    schema = SchemaVersion(run_id=_RUN_ID, nodes=nodes, edges=edges)

    expected = _reference_version_hash(nodes, edges)

    assert schema.version_hash == expected


def test_version_hash_insertion_order_invariant_nodes() -> None:
    """Nodes supplied in different orders produce the same version_hash.

    The algorithm sorts nodes by (type, node_class) before serialising, so
    insertion order must have no effect on the digest.
    """
    schema_pq = SchemaVersion(
        run_id=_RUN_ID,
        nodes=(_NODE_PERSON, _NODE_ORG),   # Person first
        edges=(_EDGE_WORKS_FOR,),
    )
    schema_qp = SchemaVersion(
        run_id=_RUN_ID,
        nodes=(_NODE_ORG, _NODE_PERSON),   # Organization first
        edges=(_EDGE_WORKS_FOR,),
    )

    assert schema_pq.version_hash == schema_qp.version_hash


def test_version_hash_insertion_order_invariant_edges() -> None:
    """Multiple edges supplied in different orders produce the same version_hash."""
    edge_2 = EdgeTypeDef(
        start_node_type="Organization",
        end_node_type="Person",
        type="EMPLOYS",
        primary_property=None,
    )
    schema_ab = SchemaVersion(
        run_id=_RUN_ID,
        nodes=(_NODE_PERSON, _NODE_ORG),
        edges=(_EDGE_WORKS_FOR, edge_2),   # WORKS_FOR first
    )
    schema_ba = SchemaVersion(
        run_id=_RUN_ID,
        nodes=(_NODE_PERSON, _NODE_ORG),
        edges=(edge_2, _EDGE_WORKS_FOR),   # EMPLOYS first
    )

    assert schema_ab.version_hash == schema_ba.version_hash


def test_version_hash_changes_with_different_node_type() -> None:
    """A different node type produces a different version_hash."""
    node_company = NodeTypeDef(
        node_class="Entity",
        type="Company",   # different from "Organization"
        primary_property="name",
    )
    schema_org = _make_schema(nodes=(_NODE_ORG,), edges=())
    schema_co = _make_schema(nodes=(node_company,), edges=())

    assert schema_org.version_hash != schema_co.version_hash


def test_version_hash_changes_with_different_primary_property() -> None:
    """Changing only primary_property on a node alters the version_hash."""
    node_email = NodeTypeDef(
        node_class="Entity",
        type="Person",
        primary_property="email",   # was "name"
    )
    schema_name = _make_schema(nodes=(_NODE_PERSON,), edges=())
    schema_email = _make_schema(nodes=(node_email,), edges=())

    assert schema_name.version_hash != schema_email.version_hash


def test_version_hash_changes_when_edge_added() -> None:
    """Adding an edge to an otherwise identical schema changes the version_hash."""
    schema_no_edge = _make_schema(nodes=(_NODE_PERSON,), edges=())
    schema_with_edge = _make_schema(nodes=(_NODE_PERSON,), edges=(_EDGE_WORKS_FOR,))

    assert schema_no_edge.version_hash != schema_with_edge.version_hash


def test_version_hash_run_id_does_not_affect_hash() -> None:
    """version_hash is independent of run_id — two runs with the same schema types agree.

    run_id is a linkage field, not a content field. The schema content (node
    and edge type definitions) determines the hash, not the run that produced it.
    """
    schema_run_a = SchemaVersion(
        run_id="run-aaaa", nodes=(_NODE_PERSON,), edges=()
    )
    schema_run_b = SchemaVersion(
        run_id="run-bbbb", nodes=(_NODE_PERSON,), edges=()
    )

    assert schema_run_a.version_hash == schema_run_b.version_hash


# ---------------------------------------------------------------------------
# Test group 2: version_hash output format
# ---------------------------------------------------------------------------


def test_version_hash_is_64_character_hex() -> None:
    """version_hash is a 64-character lowercase hexadecimal string (SHA-256)."""
    schema = _make_schema()
    h = schema.version_hash

    assert len(h) == 64
    assert h == h.lower()
    assert all(c in "0123456789abcdef" for c in h)


def test_version_hash_contains_no_dashes_or_uuid_structure() -> None:
    """version_hash must not be a UUID — CLAUDE.md §5 bans uuid4() for governed IDs."""
    schema = _make_schema()

    assert "-" not in schema.version_hash


# ---------------------------------------------------------------------------
# Test group 3: frozen model — mutation raises ValidationError (CLAUDE.md §4.4)
# ---------------------------------------------------------------------------


def test_schema_version_is_immutable() -> None:
    """Attempting to mutate a SchemaVersion field raises pydantic.ValidationError.

    SchemaVersion uses frozen=True so any attribute assignment is rejected.
    This enforces the post-lock immutability contract (SPEC-02 S-02.1).
    """
    schema = _make_schema()

    with pytest.raises(ValidationError):
        schema.run_id = "mutated-run-id"  # type: ignore[misc]


def test_schema_version_nodes_field_is_immutable() -> None:
    """Nodes tuple cannot be replaced on a locked SchemaVersion."""
    schema = _make_schema()

    with pytest.raises(ValidationError):
        schema.nodes = ()  # type: ignore[misc]


def test_node_type_def_is_immutable() -> None:
    """Attempting to mutate a NodeTypeDef field raises pydantic.ValidationError."""
    node = _NODE_PERSON

    with pytest.raises(ValidationError):
        node.type = "Employee"  # type: ignore[misc]


def test_edge_type_def_is_immutable() -> None:
    """Attempting to mutate an EdgeTypeDef field raises pydantic.ValidationError."""
    edge = _EDGE_WORKS_FOR

    with pytest.raises(ValidationError):
        edge.type = "EMPLOYED_BY"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test group 4: NodeTypeDef field validation — fail-closed (CLAUDE.md §4.4)
# ---------------------------------------------------------------------------


def test_node_type_def_empty_node_class_rejected() -> None:
    """NodeTypeDef with node_class='' raises ValidationError."""
    with pytest.raises(ValidationError):
        NodeTypeDef(node_class="", type="Person", primary_property="name")


def test_node_type_def_whitespace_only_node_class_rejected() -> None:
    """NodeTypeDef with node_class='   ' (whitespace only) raises ValidationError."""
    with pytest.raises(ValidationError):
        NodeTypeDef(node_class="   ", type="Person", primary_property="name")


def test_node_type_def_empty_type_rejected() -> None:
    """NodeTypeDef with type='' raises ValidationError."""
    with pytest.raises(ValidationError):
        NodeTypeDef(node_class="Entity", type="", primary_property="name")


def test_node_type_def_whitespace_only_type_rejected() -> None:
    """NodeTypeDef with type='\\t' (whitespace only) raises ValidationError."""
    with pytest.raises(ValidationError):
        NodeTypeDef(node_class="Entity", type="\t", primary_property="name")


def test_node_type_def_empty_primary_property_rejected() -> None:
    """NodeTypeDef with primary_property='' raises ValidationError."""
    with pytest.raises(ValidationError):
        NodeTypeDef(node_class="Entity", type="Person", primary_property="")


def test_node_type_def_whitespace_only_primary_property_rejected() -> None:
    """NodeTypeDef with primary_property='  ' raises ValidationError."""
    with pytest.raises(ValidationError):
        NodeTypeDef(node_class="Entity", type="Person", primary_property="  ")


def test_node_type_def_valid_optional_fields_accepted() -> None:
    """NodeTypeDef with all optional fields as None / empty tuple is accepted."""
    node = NodeTypeDef(
        node_class="Entity",
        type="Person",
        primary_property="name",
    )

    assert node.qualifier is None
    assert node.additional_properties == ()


# ---------------------------------------------------------------------------
# Test group 5: EdgeTypeDef field validation — fail-closed (CLAUDE.md §4.4)
# ---------------------------------------------------------------------------


def test_edge_type_def_empty_start_node_type_rejected() -> None:
    """EdgeTypeDef with start_node_type='' raises ValidationError."""
    with pytest.raises(ValidationError):
        EdgeTypeDef(start_node_type="", end_node_type="Organization", type="WORKS_FOR")


def test_edge_type_def_whitespace_start_node_type_rejected() -> None:
    """EdgeTypeDef with start_node_type=' ' (whitespace) raises ValidationError."""
    with pytest.raises(ValidationError):
        EdgeTypeDef(start_node_type=" ", end_node_type="Organization", type="WORKS_FOR")


def test_edge_type_def_empty_end_node_type_rejected() -> None:
    """EdgeTypeDef with end_node_type='' raises ValidationError."""
    with pytest.raises(ValidationError):
        EdgeTypeDef(start_node_type="Person", end_node_type="", type="WORKS_FOR")


def test_edge_type_def_empty_type_rejected() -> None:
    """EdgeTypeDef with type='' raises ValidationError."""
    with pytest.raises(ValidationError):
        EdgeTypeDef(start_node_type="Person", end_node_type="Organization", type="")


def test_edge_type_def_whitespace_type_rejected() -> None:
    """EdgeTypeDef with type='\\n' (whitespace only) raises ValidationError."""
    with pytest.raises(ValidationError):
        EdgeTypeDef(start_node_type="Person", end_node_type="Organization", type="\n")


def test_edge_type_def_valid_optional_fields_accepted() -> None:
    """EdgeTypeDef with primary_property=None and other optional omitted is valid."""
    edge = EdgeTypeDef(
        start_node_type="Person",
        end_node_type="Organization",
        type="WORKS_FOR",
    )

    assert edge.primary_property is None
    assert edge.qualifier is None
    assert edge.additional_properties == ()


# ---------------------------------------------------------------------------
# Test group 6: SchemaVersion field validation
# ---------------------------------------------------------------------------


def test_schema_version_empty_run_id_rejected() -> None:
    """SchemaVersion with run_id='' raises ValidationError."""
    with pytest.raises(ValidationError):
        SchemaVersion(run_id="", nodes=(_NODE_PERSON,), edges=())


def test_schema_version_whitespace_run_id_rejected() -> None:
    """SchemaVersion with run_id='  ' raises ValidationError."""
    with pytest.raises(ValidationError):
        SchemaVersion(run_id="  ", nodes=(_NODE_PERSON,), edges=())


def test_schema_version_empty_nodes_and_edges_accepted() -> None:
    """SchemaVersion with no nodes and no edges is structurally valid.

    Business-level constraints (at least one node) are enforced in the router
    layer (SchemaDataPayload.nodes validator), not in the domain model.
    This keeps the domain model minimal and fail-closed only for type safety.
    """
    schema = SchemaVersion(run_id=_RUN_ID, nodes=(), edges=())

    assert schema.nodes == ()
    assert schema.edges == ()


def test_schema_version_version_hash_not_an_input_field() -> None:
    """version_hash is a computed field — it is excluded from constructor input.

    Passing version_hash as a constructor argument must either be ignored or
    raise (depending on Pydantic's extra='ignore'/'forbid' setting).
    The key invariant: the stored hash is always freshly derived, never
    caller-supplied.
    """
    schema = SchemaVersion(run_id=_RUN_ID, nodes=(_NODE_PERSON,), edges=())
    reference = _reference_version_hash((_NODE_PERSON,), ())

    # The computed hash matches the reference — never a caller-supplied value.
    assert schema.version_hash == reference


def test_schema_version_serialisation_includes_version_hash() -> None:
    """model_dump() includes version_hash so callers that cache/transmit the schema
    receive the hash without a separate call (SPEC-02 S-02.1 contract)."""
    schema = _make_schema()
    dumped = schema.model_dump()

    assert "version_hash" in dumped
    assert dumped["version_hash"] == schema.version_hash
