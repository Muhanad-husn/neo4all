"""
tests/unit/test_duplicate_neighbourhood.py — Neighbourhood annotation for pairwise duplicates.

No network, no LLM, no Neo4j.  Tests verify that pairwise duplicate candidates
are annotated with neighbourhood context (how many other candidates share each
node) WITHOUT creating synthetic group candidates or suppressing any pairs.

Coverage:
  - Linear chain: no group candidates created, no suppression, all pairs survive
  - Neighbourhood counts are correct per-node
  - Neighbour value lists are populated
  - Candidates with no shared nodes get zero neighbours
  - Determinism: same input always produces same output
"""

from __future__ import annotations

from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity
from api.routers.candidates import _annotate_duplicate_neighbourhood


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


class TestNoGroupCandidates:
    """The neighbourhood annotator must never create group candidates or suppress pairs."""

    def test_chain_produces_no_groups(self) -> None:
        """A 10-node linear chain should NOT produce any group candidates."""
        nodes = [chr(65 + i) for i in range(10)]  # A..J
        candidates = [
            _make_pairwise(nodes[i], nodes[i + 1], jw=0.92)
            for i in range(9)
        ]
        original_count = len(candidates)

        _annotate_duplicate_neighbourhood(candidates)

        # No new candidates added (function mutates in-place, returns None)
        assert len(candidates) == original_count

    def test_no_suppression(self) -> None:
        """No candidate should have suppressed_by_group after annotation."""
        nodes = [chr(65 + i) for i in range(10)]
        candidates = [
            _make_pairwise(nodes[i], nodes[i + 1], jw=0.92)
            for i in range(9)
        ]

        _annotate_duplicate_neighbourhood(candidates)

        for c in candidates:
            assert "suppressed_by_group" not in c.collision_context

    def test_no_detection_method_change(self) -> None:
        """All candidates keep their original detection_method."""
        candidates = [
            _make_pairwise("A", "B"),
            _make_pairwise("B", "C"),
            _make_pairwise("C", "D"),
        ]

        _annotate_duplicate_neighbourhood(candidates)

        for c in candidates:
            assert c.detection_method == "probable_duplicate"


class TestNeighbourhoodCounts:
    """Neighbourhood counts should reflect how many OTHER candidates share each node."""

    def test_chain_middle_node_has_neighbours(self) -> None:
        """In A-B-C, node B appears in two candidates. Each should report 1 neighbour."""
        candidates = [
            _make_pairwise("A", "B"),
            _make_pairwise("B", "C"),
        ]

        _annotate_duplicate_neighbourhood(candidates)

        # A-B candidate: ref_b is B, which also appears in B-C
        ab = candidates[0]
        assert ab.collision_context["other_candidates_ref_a"] == 0  # A has no other
        assert ab.collision_context["other_candidates_ref_b"] == 1  # B also in B-C

        # B-C candidate: ref_a is B, which also appears in A-B
        bc = candidates[1]
        assert bc.collision_context["other_candidates_ref_a"] == 1  # B also in A-B
        assert bc.collision_context["other_candidates_ref_b"] == 0  # C has no other

    def test_hub_node_reports_many_neighbours(self) -> None:
        """A hub node (connected to many) should have high neighbour count."""
        # Hub: X connected to A, B, C, D
        candidates = [
            _make_pairwise("X", "A"),
            _make_pairwise("X", "B"),
            _make_pairwise("X", "C"),
            _make_pairwise("X", "D"),
        ]

        _annotate_duplicate_neighbourhood(candidates)

        # X-A candidate: X has 3 other neighbours (B, C, D)
        xa = candidates[0]
        assert xa.collision_context["other_candidates_ref_a"] == 3

    def test_isolated_pair_has_zero_neighbours(self) -> None:
        """A pair with no shared nodes gets zero neighbours."""
        candidates = [
            _make_pairwise("A", "B"),
            _make_pairwise("C", "D"),  # completely disjoint
        ]

        _annotate_duplicate_neighbourhood(candidates)

        ab = candidates[0]
        assert ab.collision_context["other_candidates_ref_a"] == 0
        assert ab.collision_context["other_candidates_ref_b"] == 0

        cd = candidates[1]
        assert cd.collision_context["other_candidates_ref_a"] == 0
        assert cd.collision_context["other_candidates_ref_b"] == 0


class TestNeighbourValues:
    """Neighbour value lists should contain the names of nodes paired with each ref."""

    def test_neighbour_values_populated(self) -> None:
        """ref_a_neighbours should list the values of other nodes paired with ref_a."""
        candidates = [
            _make_pairwise("X", "A"),
            _make_pairwise("X", "B"),
            _make_pairwise("X", "C"),
        ]

        _annotate_duplicate_neighbourhood(candidates)

        xa = candidates[0]
        # X is also paired with B and C
        assert sorted(xa.collision_context["ref_a_neighbours"]) == ["B", "C"]

    def test_neighbour_values_capped(self) -> None:
        """Neighbour lists are capped at 10 entries."""
        candidates = [
            _make_pairwise("X", chr(65 + i))  # X-A, X-B, ..., X-L (12 pairs)
            for i in range(12)
        ]

        _annotate_duplicate_neighbourhood(candidates)

        # First candidate X-A: X has 11 other neighbours, capped at 10
        xa = candidates[0]
        assert len(xa.collision_context["ref_a_neighbours"]) <= 10


class TestDeterminism:
    """Same input always produces same output."""

    def test_deterministic_output(self) -> None:
        def run() -> list[dict]:
            candidates = [
                _make_pairwise(chr(65 + i), chr(65 + i + 1))
                for i in range(10)
            ]
            _annotate_duplicate_neighbourhood(candidates)
            return [dict(c.collision_context) for c in candidates]

        assert run() == run()
