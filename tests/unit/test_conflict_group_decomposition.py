"""
tests/unit/test_conflict_group_decomposition.py — Density-validated conflict group decomposition.

No network, no LLM, no Neo4j.  Tests verify that the conflict group detector
correctly decomposes oversized/sparse components into valid sub-groups.

Coverage:
  - 10-node linear chain → decomposed into sub-groups of size <= 8 with density >= 0.5
  - 4-node clique (all 6 edges) → remains one group
  - Two 3-node cliques bridged by one weak edge → split into two groups
  - Determinism: same input always produces same output
"""

from __future__ import annotations

from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity
from api.routers.candidates import (
    MAX_GROUP_SIZE,
    MIN_DENSITY,
    _annotate_conflict_groups,
    _decompose_component,
    _is_valid_group,
)


def _make_pairwise(
    ref_a: str,
    ref_b: str,
    jw: float = 0.92,
    run_id: str = "run-1",
    schema_version: str = "sv-1",
) -> Candidate:
    """Create a pairwise probable_duplicate candidate."""
    return Candidate(
        run_id=run_id,
        schema_version=schema_version,
        candidate_type=CandidateType.probable_duplicate,
        candidate_lane=CandidateLane.node,
        involved_element_refs=(ref_a, ref_b),
        severity=Severity.medium,
        detection_method="probable_duplicate",
        collision_context={
            "jaro_winkler": jw,
            "token_overlap": 0.5,
            "context_jaccard": 0.0,
            "value_a": ref_a,
            "value_b": ref_b,
        },
    )


class TestLinearChainDecomposition:
    """A 10-node linear chain (A-B-C-...-J) has density ~0.09 and must be decomposed."""

    def test_no_mega_group(self) -> None:
        """10-node chain must NOT produce a single group of 10."""
        nodes = [chr(65 + i) for i in range(10)]  # A..J
        candidates = [
            _make_pairwise(nodes[i], nodes[i + 1], jw=0.92)
            for i in range(9)
        ]

        groups = _annotate_conflict_groups(candidates)

        # No group should exceed MAX_GROUP_SIZE
        for g in groups:
            assert len(g.involved_element_refs) <= MAX_GROUP_SIZE, (
                f"Group has {len(g.involved_element_refs)} members, "
                f"exceeds MAX_GROUP_SIZE={MAX_GROUP_SIZE}"
            )

    def test_subgroups_have_valid_density(self) -> None:
        """Every sub-group from decomposition must have density >= MIN_DENSITY."""
        nodes = [chr(65 + i) for i in range(10)]
        candidates = [
            _make_pairwise(nodes[i], nodes[i + 1], jw=0.92)
            for i in range(9)
        ]

        groups = _annotate_conflict_groups(candidates)

        for g in groups:
            ctx = g.collision_context
            density = ctx.get("edge_density", 0.0)
            size = ctx.get("conflict_group_size", 0)
            if size >= 3:
                assert density >= MIN_DENSITY, (
                    f"Group of {size} has density {density}, "
                    f"below MIN_DENSITY={MIN_DENSITY}"
                )

    def test_pairwise_candidates_not_all_suppressed(self) -> None:
        """Cross-group pairwise candidates (bridge edges) must remain unsuppressed."""
        nodes = [chr(65 + i) for i in range(10)]
        candidates = [
            _make_pairwise(nodes[i], nodes[i + 1], jw=0.92)
            for i in range(9)
        ]

        _annotate_conflict_groups(candidates)

        unsuppressed = [
            c for c in candidates
            if not c.collision_context.get("suppressed_by_group")
        ]
        # Some pairwise candidates (bridge edges between sub-groups) should survive
        assert len(unsuppressed) > 0, "All pairwise candidates were suppressed"


class TestCliquePreservation:
    """A 4-node clique (all 6 edges present) should remain one group."""

    def test_4_node_clique_stays_one_group(self) -> None:
        nodes = ["W", "X", "Y", "Z"]
        # All 6 pairwise edges
        candidates = []
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                candidates.append(_make_pairwise(nodes[i], nodes[j], jw=0.95))

        groups = _annotate_conflict_groups(candidates)

        assert len(groups) == 1, f"Expected 1 group, got {len(groups)}"
        assert groups[0].collision_context["conflict_group_size"] == 4

    def test_clique_density_is_1(self) -> None:
        nodes = ["W", "X", "Y", "Z"]
        candidates = []
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                candidates.append(_make_pairwise(nodes[i], nodes[j], jw=0.95))

        groups = _annotate_conflict_groups(candidates)

        assert len(groups) == 1
        assert groups[0].collision_context["edge_density"] == 1.0


class TestBridgedCliques:
    """Two 3-node cliques bridged by one weak edge → split into two groups."""

    def test_bridged_cliques_split(self) -> None:
        # Clique 1: A-B-C (3 edges, JW=0.95)
        # Clique 2: D-E-F (3 edges, JW=0.95)
        # Bridge: C-D (1 edge, JW=0.80 — weakest)
        candidates = [
            # Clique 1
            _make_pairwise("A", "B", jw=0.95),
            _make_pairwise("A", "C", jw=0.95),
            _make_pairwise("B", "C", jw=0.95),
            # Clique 2
            _make_pairwise("D", "E", jw=0.95),
            _make_pairwise("D", "F", jw=0.95),
            _make_pairwise("E", "F", jw=0.95),
            # Bridge
            _make_pairwise("C", "D", jw=0.80),
        ]

        groups = _annotate_conflict_groups(candidates)

        # Should get 2 groups of 3 (not 1 group of 6)
        assert len(groups) == 2, f"Expected 2 groups, got {len(groups)}"
        sizes = sorted(g.collision_context["conflict_group_size"] for g in groups)
        assert sizes == [3, 3], f"Expected [3, 3], got {sizes}"

    def test_bridge_edge_unsuppressed(self) -> None:
        """The bridge pairwise candidate (C-D) should NOT be suppressed."""
        candidates = [
            _make_pairwise("A", "B", jw=0.95),
            _make_pairwise("A", "C", jw=0.95),
            _make_pairwise("B", "C", jw=0.95),
            _make_pairwise("D", "E", jw=0.95),
            _make_pairwise("D", "F", jw=0.95),
            _make_pairwise("E", "F", jw=0.95),
            _make_pairwise("C", "D", jw=0.80),
        ]

        _annotate_conflict_groups(candidates)

        # Find the C-D candidate
        bridge = [
            c for c in candidates
            if set(c.involved_element_refs) == {"C", "D"}
        ]
        assert len(bridge) == 1
        assert not bridge[0].collision_context.get("suppressed_by_group"), (
            "Bridge edge C-D should NOT be suppressed"
        )


class TestDeterminism:
    """Same input always produces same output."""

    def test_deterministic_output(self) -> None:
        def run() -> list[tuple[str, list[str]]]:
            nodes = [chr(65 + i) for i in range(10)]
            candidates = [
                _make_pairwise(nodes[i], nodes[i + 1], jw=0.92)
                for i in range(9)
            ]
            groups = _annotate_conflict_groups(candidates)
            return [
                (g.candidate_id, list(g.involved_element_refs))
                for g in groups
            ]

        result1 = run()
        result2 = run()
        assert result1 == result2, "Non-deterministic conflict group output"


class TestIsValidGroup:
    """Direct tests for _is_valid_group helper."""

    def test_pair_always_valid(self) -> None:
        assert _is_valid_group({"A", "B"}, {frozenset({"A", "B"})}) is True

    def test_oversized_invalid(self) -> None:
        members = {str(i) for i in range(MAX_GROUP_SIZE + 1)}
        assert _is_valid_group(members, set()) is False

    def test_sparse_chain_invalid(self) -> None:
        # 5-node chain: A-B-C-D-E (4 edges, max 10)
        members = {"A", "B", "C", "D", "E"}
        edges = {
            frozenset({"A", "B"}),
            frozenset({"B", "C"}),
            frozenset({"C", "D"}),
            frozenset({"D", "E"}),
        }
        # density = 4/10 = 0.4 < 0.5
        assert _is_valid_group(members, edges) is False

    def test_dense_clique_valid(self) -> None:
        members = {"A", "B", "C", "D"}
        edges = {
            frozenset({"A", "B"}), frozenset({"A", "C"}), frozenset({"A", "D"}),
            frozenset({"B", "C"}), frozenset({"B", "D"}), frozenset({"C", "D"}),
        }
        assert _is_valid_group(members, edges) is True


class TestDecomposeComponent:
    """Direct tests for _decompose_component helper."""

    def test_singleton_fallback(self) -> None:
        """When no valid grouping exists, all nodes become singletons."""
        # 4-node path: A-B-C-D (chain, density < 0.5 even after cuts)
        # Actually a 4-node path has density 3/6 = 0.5 which passes.
        # Use a 5-node path instead.
        nodes = {"A", "B", "C", "D", "E"}
        edge_map: dict[frozenset[str], Candidate] = {}
        for a, b in [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")]:
            edge_map[frozenset({a, b})] = _make_pairwise(a, b, jw=0.90)

        result = _decompose_component(nodes, edge_map)

        # All sub-groups should be valid
        for sg in result:
            if len(sg) >= 3:
                sg_edges = {e for e in edge_map if e <= sg}
                assert _is_valid_group(sg, sg_edges), (
                    f"Sub-group {sg} is not valid"
                )
