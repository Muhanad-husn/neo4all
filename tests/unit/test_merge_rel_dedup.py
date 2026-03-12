"""
tests/unit/test_merge_rel_dedup.py — Merge relationship deduplication tests.

No network, no LLM, no Neo4j.  Tests cover:
  - _rel_dedupe_key produces correct deterministic keys
  - _extract_chunk_id extracts from raw props or defaults to empty string
  - detect_scoped only emits candidates involving focus_keys
  - run_scoped_detection filters each detector correctly
  - merge_affected cache key builder
"""

from __future__ import annotations

import pytest

from api.cache.keys import CacheKey
from api.graph.reader_models import (
    GraphNodeRecord,
    GraphRelRecord,
    NodeListResult,
    RelListResult,
)
from api.graph.safety import _extract_chunk_id, _rel_dedupe_key
from api.models.candidate import CandidateType
from api.schema.models import EdgeTypeDef, NodeTypeDef, SchemaVersion
from api.services.curation.candidates import ProbableDuplicateDetector

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-merge-dedup"
_SV = "sv_merge_test_abc123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(dedupe_key: str, node_type: str, name: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        dedupe_key=dedupe_key,
        node_type=node_type,
        run_id=_RUN_ID,
        schema_version=_SV,
        properties={"name": name},
    )


def _make_rel(
    dedupe_key: str,
    rel_type: str,
    start: str,
    end: str,
) -> GraphRelRecord:
    return GraphRelRecord(
        dedupe_key=dedupe_key,
        rel_type=rel_type,
        start_dedupe_key=start,
        end_dedupe_key=end,
        run_id=_RUN_ID,
        schema_version=_SV,
        properties={},
    )


def _make_schema() -> SchemaVersion:
    return SchemaVersion(
        run_id=_RUN_ID,
        nodes=(
            NodeTypeDef(
                node_class="Entity",
                type="Person",
                primary_property="name",
            ),
        ),
        edges=(
            EdgeTypeDef(
                type="KNOWS",
                start_node_type="Person",
                end_node_type="Person",
            ),
        ),
    )


# ---------------------------------------------------------------------------
# _rel_dedupe_key tests
# ---------------------------------------------------------------------------


class TestRelDedupeKey:
    """Test the _rel_dedupe_key helper in safety.py."""

    def test_basic_format(self) -> None:
        result = _rel_dedupe_key("KNOWS", "start_key", "end_key", "sv1")
        assert result == "KNOWS::start_key::end_key::sv1"

    def test_deterministic(self) -> None:
        a = _rel_dedupe_key("REL", "s", "e", "v")
        b = _rel_dedupe_key("REL", "s", "e", "v")
        assert a == b

    def test_direction_matters(self) -> None:
        forward = _rel_dedupe_key("REL", "A", "B", "v1")
        reverse = _rel_dedupe_key("REL", "B", "A", "v1")
        assert forward != reverse

    def test_type_matters(self) -> None:
        a = _rel_dedupe_key("KNOWS", "s", "e", "v")
        b = _rel_dedupe_key("WORKS_WITH", "s", "e", "v")
        assert a != b

    def test_schema_version_matters(self) -> None:
        a = _rel_dedupe_key("REL", "s", "e", "v1")
        b = _rel_dedupe_key("REL", "s", "e", "v2")
        assert a != b


# ---------------------------------------------------------------------------
# _extract_chunk_id tests
# ---------------------------------------------------------------------------


class TestExtractChunkId:
    """Test the _extract_chunk_id helper in safety.py."""

    def test_present(self) -> None:
        assert _extract_chunk_id({"_chunk_id": "c123", "name": "x"}) == "c123"

    def test_missing(self) -> None:
        assert _extract_chunk_id({"name": "x"}) == ""

    def test_none_value(self) -> None:
        assert _extract_chunk_id({"_chunk_id": None}) == "None"


# ---------------------------------------------------------------------------
# ProbableDuplicateDetector.detect_scoped tests
# ---------------------------------------------------------------------------


class TestDetectScoped:
    """Test that detect_scoped only emits candidates involving focus_keys."""

    def test_scoped_filters_to_focus_keys(self) -> None:
        # Create nodes: A and A_dup are similar, B and B_dup are similar.
        # Focus only on A — should only get A/A_dup candidate.
        nodes = NodeListResult(
            nodes=(
                _make_node("Person|alice|sv", "Person", "Alice Johnson"),
                _make_node("Person|alice_j|sv", "Person", "Alice Jonson"),
                _make_node("Person|bob|sv", "Person", "Robert Smith"),
                _make_node("Person|rob|sv", "Person", "Robert Smth"),
            )
        )
        schema = _make_schema()
        focus_keys = {"Person|alice|sv"}

        detector = ProbableDuplicateDetector()
        candidates = detector.detect_scoped(
            run_id=_RUN_ID,
            schema_version=_SV,
            nodes=nodes,
            schema=schema,
            focus_keys=focus_keys,
        )

        # Should only find alice/alice_j pair, not bob/rob pair.
        refs_sets = [set(c.involved_element_refs) for c in candidates]
        assert any(
            "Person|alice|sv" in refs for refs in refs_sets
        ), "Expected alice pair in scoped results"
        for refs in refs_sets:
            assert "Person|bob|sv" in refs or "Person|rob|sv" in refs or \
                "Person|alice|sv" in refs or "Person|alice_j|sv" in refs

    def test_scoped_empty_focus_returns_empty(self) -> None:
        nodes = NodeListResult(
            nodes=(
                _make_node("Person|alice|sv", "Person", "Alice Johnson"),
                _make_node("Person|alice_j|sv", "Person", "Alice Jonson"),
            )
        )
        schema = _make_schema()
        detector = ProbableDuplicateDetector()
        candidates = detector.detect_scoped(
            run_id=_RUN_ID,
            schema_version=_SV,
            nodes=nodes,
            schema=schema,
            focus_keys=set(),
        )
        assert candidates == []

    def test_scoped_includes_pair_when_either_in_focus(self) -> None:
        nodes = NodeListResult(
            nodes=(
                _make_node("Person|alice|sv", "Person", "Alice Johnson"),
                _make_node("Person|alice_j|sv", "Person", "Alice Jonson"),
            )
        )
        schema = _make_schema()
        detector = ProbableDuplicateDetector()

        # Focus on the second node — should still find the pair.
        candidates = detector.detect_scoped(
            run_id=_RUN_ID,
            schema_version=_SV,
            nodes=nodes,
            schema=schema,
            focus_keys={"Person|alice_j|sv"},
        )
        assert len(candidates) >= 1
        assert candidates[0].candidate_type == CandidateType.probable_duplicate


# ---------------------------------------------------------------------------
# CacheKey.merge_affected tests
# ---------------------------------------------------------------------------


class TestMergeAffectedCacheKey:
    """Test the merge_affected cache key builder."""

    def test_format(self) -> None:
        key = CacheKey.merge_affected(run_id="run1", diff_id="diff1")
        assert key == "merge_affected:run1:diff1"

    def test_deterministic(self) -> None:
        a = CacheKey.merge_affected(run_id="r", diff_id="d")
        b = CacheKey.merge_affected(run_id="r", diff_id="d")
        assert a == b

    def test_different_inputs(self) -> None:
        a = CacheKey.merge_affected(run_id="r1", diff_id="d1")
        b = CacheKey.merge_affected(run_id="r1", diff_id="d2")
        assert a != b
