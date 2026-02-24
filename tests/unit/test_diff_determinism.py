"""
tests/unit/test_diff_determinism.py — DiffBuilder determinism tests (SPEC-06 file 15).

No network, no LLM, no Neo4j.  DiffBuilder is a pure in-process function with
no external dependencies.

Coverage:
  - Same proposal → same diff_id on repeated calls (all six proposal classes)
  - DiffBuilder is stateless; two instances produce the same diff_id
  - Property dict key order does not affect diff_id (sorted in stable_dict)
  - Target declaration order does not affect diff_id for canonicalize/normalize/rename
  - Merge preserves declaration order: targets[0] is always the survivor
  - Delete: edge steps precede node steps; each group sorted by dedupe_key
  - Defer: empty steps, stable diff_id (hash of empty list)
  - Empty targets: well-formed plan with no steps; diff_id stable
  - DiffPlan carries correct linkage fields from ProposalPacket
  - diff_id is always a 64-character lowercase hex SHA-256 digest
  - Different proposals produce different diff_ids
  - Fixture round-trip: sample_proposal.json → DiffBuilder → matches sample_diff.json
"""

from __future__ import annotations

import json
import pathlib

import pytest

from api.diff.builder import DiffBuilder
from api.diff.models import DiffOperation
from api.proposals.models import ElementRef, ProposalClass, ProposalPacket

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES = pathlib.Path(__file__).parent.parent.parent / "fixtures" / "curation"
_RUN_ID = "unit-test-run-diff-det-0001"
_CANDIDATE_ID = "a" * 64
_SCHEMA_VERSION = "sv_diff_det_fixture_v1"
_BUILDER = DiffBuilder()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proposal(
    proposal_class: ProposalClass,
    targets: list[dict] | None = None,
    rationale: str = "test determinism rationale",
    rule_ids: tuple[str, ...] = ("R-TEST-01",),
) -> ProposalPacket:
    """Construct a minimal but valid ProposalPacket for testing."""
    return ProposalPacket(
        run_id=_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        proposal_class=proposal_class,
        schema_version=_SCHEMA_VERSION,
        rationale=rationale,
        rule_ids=rule_ids,
        targets=tuple(ElementRef(**t) for t in (targets or [])),
    )


def _node(dedupe_key: str, properties: dict | None = None) -> dict:
    return {
        "element_type": "node",
        "dedupe_key": dedupe_key,
        "label": dedupe_key,
        "properties": properties or {},
    }


def _rel(dedupe_key: str) -> dict:
    return {
        "element_type": "relationship",
        "dedupe_key": dedupe_key,
        "label": dedupe_key,
    }


# ---------------------------------------------------------------------------
# Core determinism: same proposal → same diff_id (all proposal classes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proposal_class,targets",
    [
        (ProposalClass.canonicalize, [_node("A|alice|v1", {"name": "Alice"})]),
        (ProposalClass.normalize, [_node("B|bob|v1", {"status": "active"})]),
        (ProposalClass.rename, [_node("C|carol|v1", {"name": "Carol"})]),
        (ProposalClass.merge, [_node("A|alice|v1"), _node("A|alice2|v1")]),
        (ProposalClass.delete, [_node("A|alice|v1"), _rel("R::a::b::v1")]),
        (ProposalClass.defer, []),
    ],
    ids=["canonicalize", "normalize", "rename", "merge", "delete", "defer"],
)
def test_same_proposal_same_diff_id(
    proposal_class: ProposalClass, targets: list[dict]
) -> None:
    """Calling build() twice on the identical proposal always produces the same diff_id."""
    proposal = _make_proposal(proposal_class, targets)
    plan_a = _BUILDER.build(proposal)
    plan_b = _BUILDER.build(proposal)
    assert plan_a.diff_id == plan_b.diff_id


def test_diff_id_independent_of_builder_instance() -> None:
    """DiffBuilder is stateless; two separate instances produce the same diff_id."""
    proposal = _make_proposal(
        ProposalClass.canonicalize, [_node("X|xray|v1", {"key": "value"})]
    )
    plan_a = DiffBuilder().build(proposal)
    plan_b = DiffBuilder().build(proposal)
    assert plan_a.diff_id == plan_b.diff_id


# ---------------------------------------------------------------------------
# Property dict key order does not affect diff_id
# ---------------------------------------------------------------------------


def test_property_key_order_stable() -> None:
    """Properties dict key order is sorted in _stable_dict — diff_id must be equal."""
    targets_unsorted = [_node("X|node|v1", {"z": 3, "a": 1, "m": 2})]
    targets_sorted = [_node("X|node|v1", {"a": 1, "m": 2, "z": 3})]

    plan_unsorted = _BUILDER.build(_make_proposal(ProposalClass.canonicalize, targets_unsorted))
    plan_sorted = _BUILDER.build(_make_proposal(ProposalClass.canonicalize, targets_sorted))

    assert plan_unsorted.diff_id == plan_sorted.diff_id


# ---------------------------------------------------------------------------
# Target ordering: sorted by dedupe_key for canonicalize / normalize / rename
# ---------------------------------------------------------------------------


def test_canonicalize_target_order_invariant() -> None:
    """Reversed target order produces identical diff_id (builder sorts by dedupe_key)."""
    targets_forward = [
        _node("A|alice|v1", {"name": "Alice"}),
        _node("B|bob|v1", {"name": "Bob"}),
    ]
    targets_reversed = [
        _node("B|bob|v1", {"name": "Bob"}),
        _node("A|alice|v1", {"name": "Alice"}),
    ]
    plan_fwd = _BUILDER.build(_make_proposal(ProposalClass.canonicalize, targets_forward))
    plan_rev = _BUILDER.build(_make_proposal(ProposalClass.canonicalize, targets_reversed))

    assert plan_fwd.diff_id == plan_rev.diff_id
    # Steps are always in dedupe_key order
    assert plan_fwd.steps[0].dedupe_key == "A|alice|v1"
    assert plan_fwd.steps[1].dedupe_key == "B|bob|v1"


def test_normalize_target_order_invariant() -> None:
    """normalize produces the same diff_id regardless of target input order."""
    targets = [_node("Z|zara|v1", {"x": 1}), _node("A|anna|v1", {"x": 2})]
    reversed_targets = list(reversed(targets))

    plan_a = _BUILDER.build(_make_proposal(ProposalClass.normalize, targets))
    plan_b = _BUILDER.build(_make_proposal(ProposalClass.normalize, reversed_targets))

    assert plan_a.diff_id == plan_b.diff_id


def test_rename_target_order_invariant() -> None:
    """rename produces the same diff_id regardless of target input order."""
    targets = [_node("C|carol|v1", {"name": "Carol"}), _node("A|anna|v1", {"name": "Anna"})]
    reversed_targets = list(reversed(targets))

    plan_a = _BUILDER.build(_make_proposal(ProposalClass.rename, targets))
    plan_b = _BUILDER.build(_make_proposal(ProposalClass.rename, reversed_targets))

    assert plan_a.diff_id == plan_b.diff_id


# ---------------------------------------------------------------------------
# Merge: declaration order preserved (survivor = targets[0])
# ---------------------------------------------------------------------------


def test_merge_survivor_is_first_target() -> None:
    """Swapping survivor and obsolete node changes the diff_id."""
    targets_a_first = [_node("A|alice|v1"), _node("B|bob|v1")]
    targets_b_first = [_node("B|bob|v1"), _node("A|alice|v1")]

    plan_a = _BUILDER.build(_make_proposal(ProposalClass.merge, targets_a_first))
    plan_b = _BUILDER.build(_make_proposal(ProposalClass.merge, targets_b_first))

    assert plan_a.diff_id != plan_b.diff_id
    assert plan_a.steps[0].merge_keys[0] == "A|alice|v1"
    assert plan_b.steps[0].merge_keys[0] == "B|bob|v1"


def test_merge_produces_exactly_one_step() -> None:
    """A merge proposal produces exactly one merge_nodes step regardless of target count."""
    proposal = _make_proposal(
        ProposalClass.merge,
        [_node("A|alice|v1"), _node("B|bob|v1"), _node("C|carol|v1")],
    )
    plan = _BUILDER.build(proposal)
    assert len(plan.steps) == 1
    assert plan.steps[0].operation == DiffOperation.merge_nodes


def test_merge_all_dedupe_keys_in_merge_keys() -> None:
    """All target dedupe_keys appear in merge_keys in declaration order."""
    proposal = _make_proposal(
        ProposalClass.merge,
        [_node("P|primary|v1"), _node("Q|secondary|v1"), _node("R|tertiary|v1")],
    )
    plan = _BUILDER.build(proposal)
    assert list(plan.steps[0].merge_keys) == [
        "P|primary|v1",
        "Q|secondary|v1",
        "R|tertiary|v1",
    ]


# ---------------------------------------------------------------------------
# Delete: edges before nodes, each group sorted by dedupe_key
# ---------------------------------------------------------------------------


def test_delete_edges_before_nodes() -> None:
    """Delete proposals place all edge steps before all node steps."""
    proposal = _make_proposal(
        ProposalClass.delete,
        [
            _node("A|alice|v1"),
            _rel("KNOWS::a::b::v1"),
            _node("B|bob|v1"),
            _rel("WORKS::c::d::v1"),
        ],
    )
    plan = _BUILDER.build(proposal)
    operations = [s.operation for s in plan.steps]
    edge_idxs = [i for i, op in enumerate(operations) if op == DiffOperation.delete_edge]
    node_idxs = [i for i, op in enumerate(operations) if op == DiffOperation.delete_node]

    assert edge_idxs and node_idxs
    assert max(edge_idxs) < min(node_idxs)


def test_delete_steps_sorted_within_each_group() -> None:
    """Within edge steps and node steps, ordering is by ascending dedupe_key."""
    proposal = _make_proposal(
        ProposalClass.delete,
        [
            _node("Z|zara|v1"),
            _node("A|anna|v1"),
            _rel("Z::z::z::v1"),
            _rel("A::a::a::v1"),
        ],
    )
    plan = _BUILDER.build(proposal)
    edge_steps = [s for s in plan.steps if s.operation == DiffOperation.delete_edge]
    node_steps = [s for s in plan.steps if s.operation == DiffOperation.delete_node]

    assert edge_steps[0].dedupe_key < edge_steps[1].dedupe_key
    assert node_steps[0].dedupe_key < node_steps[1].dedupe_key


# ---------------------------------------------------------------------------
# Defer: empty steps, stable diff_id
# ---------------------------------------------------------------------------


def test_defer_produces_no_steps() -> None:
    """A defer proposal produces a well-formed plan with an empty steps tuple."""
    plan = _BUILDER.build(_make_proposal(ProposalClass.defer))
    assert plan.steps == ()


def test_defer_diff_id_stable_across_calls() -> None:
    """Defer diff_id is always the same (hash of empty list)."""
    proposal = _make_proposal(ProposalClass.defer)
    assert _BUILDER.build(proposal).diff_id == _BUILDER.build(proposal).diff_id


# ---------------------------------------------------------------------------
# Empty targets
# ---------------------------------------------------------------------------


def test_empty_targets_produces_no_steps() -> None:
    """Empty targets for canonicalize produce an empty steps tuple."""
    plan = _BUILDER.build(_make_proposal(ProposalClass.canonicalize, []))
    assert plan.steps == ()


def test_empty_targets_diff_id_stable() -> None:
    """Empty targets produce a stable diff_id across calls."""
    proposal = _make_proposal(ProposalClass.canonicalize, [])
    assert _BUILDER.build(proposal).diff_id == _BUILDER.build(proposal).diff_id


# ---------------------------------------------------------------------------
# DiffPlan carries correct linkage from ProposalPacket
# ---------------------------------------------------------------------------


def test_diff_plan_carries_proposal_linkage() -> None:
    """DiffPlan.run_id, candidate_id, schema_version, proposal_id mirror the proposal."""
    proposal = _make_proposal(
        ProposalClass.canonicalize, [_node("X|x|v1", {"k": "v"})]
    )
    plan = _BUILDER.build(proposal)
    assert plan.run_id == _RUN_ID
    assert plan.candidate_id == _CANDIDATE_ID
    assert plan.schema_version == _SCHEMA_VERSION
    assert plan.proposal_id == proposal.proposal_id


def test_diff_id_is_64_char_hex() -> None:
    """diff_id is always a 64-character lowercase hexadecimal SHA-256 digest."""
    plan = _BUILDER.build(
        _make_proposal(ProposalClass.canonicalize, [_node("X|y|v1", {"a": 1})])
    )
    assert len(plan.diff_id) == 64
    assert all(c in "0123456789abcdef" for c in plan.diff_id)


# ---------------------------------------------------------------------------
# Different proposals → different diff_ids
# ---------------------------------------------------------------------------


def test_different_step_structure_different_diff_id() -> None:
    """Proposals that produce structurally different steps produce different diff_ids.

    Note: diff_id is the hash of *steps*, not the proposal.  Two classes that
    produce the same step shapes (e.g. canonicalize/normalize both use
    update_node) will produce the same diff_id for identical targets.
    This test uses canonicalize (update_node) vs defer (empty) to guarantee a
    structural difference.
    """
    target = [_node("A|a|v1", {"k": "v"})]
    plan_canon = _BUILDER.build(_make_proposal(ProposalClass.canonicalize, target))
    plan_defer = _BUILDER.build(_make_proposal(ProposalClass.defer, target))
    # canonicalize → one update_node step; defer → zero steps → different hashes
    assert plan_canon.diff_id != plan_defer.diff_id


def test_different_properties_different_diff_id() -> None:
    """Two proposals with different property values produce different diff_ids."""
    plan_a = _BUILDER.build(
        _make_proposal(ProposalClass.canonicalize, [_node("X|x|v1", {"name": "Alice"})])
    )
    plan_b = _BUILDER.build(
        _make_proposal(ProposalClass.canonicalize, [_node("X|x|v1", {"name": "Bob"})])
    )
    assert plan_a.diff_id != plan_b.diff_id


# ---------------------------------------------------------------------------
# Fixture round-trip
# ---------------------------------------------------------------------------


def test_fixture_diff_round_trip() -> None:
    """sample_proposal.json → DiffBuilder → matches sample_diff.json expectations."""
    proposal_fix = json.loads(
        (_FIXTURES / "sample_proposal.json").read_text(encoding="utf-8")
    )
    diff_fix = json.loads(
        (_FIXTURES / "sample_diff.json").read_text(encoding="utf-8")
    )

    proposal = ProposalPacket(
        run_id=proposal_fix["run_id"],
        candidate_id=proposal_fix["candidate_id"],
        proposal_class=ProposalClass(proposal_fix["proposal_class"]),
        schema_version=proposal_fix["schema_version"],
        rationale=proposal_fix["rationale"],
        rule_ids=tuple(proposal_fix.get("rule_ids", [])),
        targets=tuple(ElementRef(**t) for t in proposal_fix["targets"]),
    )
    plan = _BUILDER.build(proposal)

    # Structural match against fixture expectations
    assert len(plan.steps) == diff_fix["expected_step_count"]

    actual_ops = [str(s.operation) for s in plan.steps]
    assert actual_ops == diff_fix["expected_operations"]

    for i, expected in enumerate(diff_fix["expected_step_order"]):
        assert plan.steps[i].dedupe_key == expected["dedupe_key"]
        assert plan.steps[i].properties_set == expected["properties_set"]

    # diff_id must be stable across two builds from the same proposal
    plan2 = _BUILDER.build(proposal)
    assert plan.diff_id == plan2.diff_id
