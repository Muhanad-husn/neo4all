"""
tests/unit/test_canonical_violation_amendment.py — Detector tests with schema amendments.

Verifies that CanonicalViolationDetector correctly uses amendment_edges to
suppress violations for edge directions that have been accepted as valid.

No network, no LLM, no Neo4j — pure detector logic with fixture data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.graph.reader_models import (
    GraphNodeRecord,
    GraphRelRecord,
    NodeListResult,
    RelListResult,
)
from api.schema.models import EdgeTypeDef, NodeTypeDef, SchemaVersion
from api.services.curation.candidates import CanonicalViolationDetector

# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"
_AMENDMENT_FIXTURE = _FIXTURES_DIR / "schema_amendment" / "amendment_scenario.json"
_CANONICAL_FIXTURE = _FIXTURES_DIR / "candidate_detection" / "canonical_violation.json"


def _load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_schema(data: dict) -> SchemaVersion:
    sd = data["schema"]
    return SchemaVersion(
        run_id=sd["run_id"],
        nodes=tuple(
            NodeTypeDef(**n) for n in sd["nodes"]
        ),
        edges=tuple(
            EdgeTypeDef(**e) for e in sd["edges"]
        ),
    )


def _build_nodes(data: dict) -> NodeListResult:
    return NodeListResult(
        nodes=[GraphNodeRecord(**n) for n in data["nodes"]]
    )


def _build_rels(data: dict) -> RelListResult:
    return RelListResult(
        rels=[GraphRelRecord(**r) for r in data["rels"]]
    )


# ---------------------------------------------------------------------------
# Tests — existing canonical violation fixture (regression)
# ---------------------------------------------------------------------------


class TestCanonicalViolationWithoutAmendments:
    """Existing detector behaviour is unchanged when amendment_edges is empty."""

    @pytest.fixture()
    def fixture(self) -> dict:
        return _load_fixture(_CANONICAL_FIXTURE)

    def test_violations_detected_without_amendments(self, fixture: dict) -> None:
        """Default (no amendment_edges) still detects violations."""
        schema = _build_schema(fixture)
        nodes = _build_nodes(fixture)
        rels = _build_rels(fixture)

        candidates = CanonicalViolationDetector().detect(
            run_id=fixture["run_id"],
            schema_version=fixture["schema_version_str"],
            rels=rels,
            nodes=nodes,
            schema=schema,
        )

        assert len(candidates) == len(fixture["expected_candidates"])

    def test_violations_detected_with_empty_amendments(self, fixture: dict) -> None:
        """Explicit empty amendment_edges tuple produces same result as default."""
        schema = _build_schema(fixture)
        nodes = _build_nodes(fixture)
        rels = _build_rels(fixture)

        candidates = CanonicalViolationDetector().detect(
            run_id=fixture["run_id"],
            schema_version=fixture["schema_version_str"],
            rels=rels,
            nodes=nodes,
            schema=schema,
            amendment_edges=(),
        )

        assert len(candidates) == len(fixture["expected_candidates"])


# ---------------------------------------------------------------------------
# Tests — amendment scenario fixture
# ---------------------------------------------------------------------------


class TestCanonicalViolationWithAmendments:
    """Detector correctly uses amendment_edges to suppress resolved violations."""

    @pytest.fixture()
    def fixture(self) -> dict:
        return _load_fixture(_AMENDMENT_FIXTURE)

    def test_violation_before_amendment(self, fixture: dict) -> None:
        """Without the amendment, the inverse violation is detected."""
        schema = _build_schema(fixture)
        nodes = _build_nodes(fixture)
        rels = _build_rels(fixture)

        candidates = CanonicalViolationDetector().detect(
            run_id=fixture["run_id"],
            schema_version=fixture["schema_version_str"],
            rels=rels,
            nodes=nodes,
            schema=schema,
        )

        expected_count = fixture["expected_before_amendment"]["violation_count"]
        assert len(candidates) == expected_count
        # The violation should be an inverse violation
        assert candidates[0].detection_method == "canonical_inverse_violation"

    def test_violation_disappears_after_amendment(self, fixture: dict) -> None:
        """With the amendment edge, the inverse violation disappears."""
        schema = _build_schema(fixture)
        nodes = _build_nodes(fixture)
        rels = _build_rels(fixture)

        amendment_edge = EdgeTypeDef(**fixture["amendment_edge"])
        candidates = CanonicalViolationDetector().detect(
            run_id=fixture["run_id"],
            schema_version=fixture["schema_version_str"],
            rels=rels,
            nodes=nodes,
            schema=schema,
            amendment_edges=(amendment_edge,),
        )

        expected_count = fixture["expected_after_amendment"]["violation_count"]
        assert len(candidates) == expected_count

    def test_amendment_only_affects_matching_direction(self, fixture: dict) -> None:
        """An amendment for a different edge type does not suppress violations."""
        schema = _build_schema(fixture)
        nodes = _build_nodes(fixture)
        rels = _build_rels(fixture)

        # Amend MENTORS instead of WORKS_FOR — shouldn't help
        wrong_amendment = EdgeTypeDef(
            start_node_type="Organization",
            end_node_type="Person",
            type="MENTORS",
        )
        candidates = CanonicalViolationDetector().detect(
            run_id=fixture["run_id"],
            schema_version=fixture["schema_version_str"],
            rels=rels,
            nodes=nodes,
            schema=schema,
            amendment_edges=(wrong_amendment,),
        )

        # The WORKS_FOR violation should still be detected.
        assert len(candidates) == fixture["expected_before_amendment"]["violation_count"]

    def test_multiple_amendments(self, fixture: dict) -> None:
        """Multiple amendment edges can be passed together."""
        schema = _build_schema(fixture)
        nodes = _build_nodes(fixture)
        rels = _build_rels(fixture)

        amendment_edge_1 = EdgeTypeDef(**fixture["amendment_edge"])
        amendment_edge_2 = EdgeTypeDef(
            start_node_type="Organization",
            end_node_type="Person",
            type="MENTORS",
        )

        candidates = CanonicalViolationDetector().detect(
            run_id=fixture["run_id"],
            schema_version=fixture["schema_version_str"],
            rels=rels,
            nodes=nodes,
            schema=schema,
            amendment_edges=(amendment_edge_1, amendment_edge_2),
        )

        assert len(candidates) == 0


# ---------------------------------------------------------------------------
# Tests — canonical violation fixture with targeted amendment
# ---------------------------------------------------------------------------


class TestCanonicalViolationFixtureWithAmendment:
    """Use the original canonical_violation.json fixture and amend one violation."""

    @pytest.fixture()
    def fixture(self) -> dict:
        return _load_fixture(_CANONICAL_FIXTURE)

    def test_amending_inverse_violation(self, fixture: dict) -> None:
        """Amending WORKS_FOR: Organization→Person resolves the inverse violation.

        The direction violation (MENTORS: Organization→Person) should remain.
        """
        schema = _build_schema(fixture)
        nodes = _build_nodes(fixture)
        rels = _build_rels(fixture)

        # Amend the inverse violation direction.
        amendment = EdgeTypeDef(
            start_node_type="Organization",
            end_node_type="Person",
            type="WORKS_FOR",
        )

        candidates = CanonicalViolationDetector().detect(
            run_id=fixture["run_id"],
            schema_version=fixture["schema_version_str"],
            rels=rels,
            nodes=nodes,
            schema=schema,
            amendment_edges=(amendment,),
        )

        # Only the MENTORS direction violation should remain.
        assert len(candidates) == 1
        assert candidates[0].detection_method == "canonical_direction_violation"
        assert candidates[0].collision_context["rel_type"] == "MENTORS"

    def test_amending_direction_violation(self, fixture: dict) -> None:
        """Amending MENTORS: Organization→Person resolves the direction violation.

        The inverse violation (WORKS_FOR) should remain.
        """
        schema = _build_schema(fixture)
        nodes = _build_nodes(fixture)
        rels = _build_rels(fixture)

        amendment = EdgeTypeDef(
            start_node_type="Organization",
            end_node_type="Person",
            type="MENTORS",
        )

        candidates = CanonicalViolationDetector().detect(
            run_id=fixture["run_id"],
            schema_version=fixture["schema_version_str"],
            rels=rels,
            nodes=nodes,
            schema=schema,
            amendment_edges=(amendment,),
        )

        assert len(candidates) == 1
        assert candidates[0].detection_method == "canonical_inverse_violation"
        assert candidates[0].collision_context["rel_type"] == "WORKS_FOR"

    def test_amending_both_resolves_all(self, fixture: dict) -> None:
        """Amending both violations leaves zero candidates."""
        schema = _build_schema(fixture)
        nodes = _build_nodes(fixture)
        rels = _build_rels(fixture)

        amendments = (
            EdgeTypeDef(
                start_node_type="Organization",
                end_node_type="Person",
                type="WORKS_FOR",
            ),
            EdgeTypeDef(
                start_node_type="Organization",
                end_node_type="Person",
                type="MENTORS",
            ),
        )

        candidates = CanonicalViolationDetector().detect(
            run_id=fixture["run_id"],
            schema_version=fixture["schema_version_str"],
            rels=rels,
            nodes=nodes,
            schema=schema,
            amendment_edges=amendments,
        )

        assert len(candidates) == 0
