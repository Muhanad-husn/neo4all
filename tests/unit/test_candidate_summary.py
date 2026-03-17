"""Unit tests for ui.components.candidate_summary."""


from ui.components.candidate_summary import (
    method_label,
    short_summary,
    summarize_candidate,
)


# ---------------------------------------------------------------------------
# method_label
# ---------------------------------------------------------------------------


class TestMethodLabel:
    def test_exact_node(self):
        assert method_label("exact_node_dedupe_key") == "Exact node duplicate"

    def test_exact_rel(self):
        assert method_label("exact_rel_dedupe_key") == "Exact relationship duplicate"

    def test_orphan(self):
        assert method_label("orphan_node") == "Orphan node"

    def test_jaro_winkler_prefix(self):
        assert method_label("jaro_winkler_0.90") == "Fuzzy match"
        assert method_label("jaro_winkler_0.85") == "Fuzzy match"

    def test_unknown_method(self):
        result = method_label("some_new_detector")
        assert result == "Some New Detector"

    def test_empty_string(self):
        result = method_label("")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# summarize_candidate
# ---------------------------------------------------------------------------


def _candidate(method: str, ctx: dict | None = None, refs: list | None = None) -> dict:
    return {
        "candidate_id": "abc123",
        "detection_method": method,
        "collision_context": ctx or {},
        "involved_element_refs": refs or ["ref1", "ref2"],
        "severity": "medium",
        "candidate_type": "exact_node_duplicate",
    }


class TestSummarizeCandidate:
    def test_exact_node(self):
        c = _candidate("exact_node_dedupe_key", {"node_type": "Person"}, ["a", "b", "c"])
        result = summarize_candidate(c)
        assert "3" in result
        assert "Person" in result
        assert "exact duplicate" in result

    def test_exact_rel(self):
        c = _candidate("exact_rel_dedupe_key", {"rel_type": "WORKS_AT"}, ["a", "b"])
        result = summarize_candidate(c)
        assert "WORKS_AT" in result
        assert "duplicate" in result

    def test_jaro_winkler_with_names(self):
        c = _candidate(
            "jaro_winkler_0.95",
            {"node_type": "Person", "names": ["John Smith", "Jon Smith"]},
        )
        result = summarize_candidate(c)
        assert "John Smith" in result
        assert "Jon Smith" in result
        assert "95%" in result

    def test_jaro_winkler_without_names(self):
        c = _candidate("jaro_winkler_0.90", {"node_type": "Person"})
        result = summarize_candidate(c)
        assert "Possible duplicate" in result
        assert "Person" in result

    def test_canonical_inverse(self):
        c = _candidate(
            "canonical_inverse_violation",
            {
                "rel_type": "WORKS_AT",
                "start_label": "Company",
                "end_label": "Person",
                "expected_start": "Person",
                "expected_end": "Company",
            },
        )
        result = summarize_candidate(c)
        assert "WORKS_AT" in result
        assert "Company" in result
        assert "Person" in result

    def test_canonical_direction(self):
        c = _candidate(
            "canonical_direction_violation",
            {"rel_type": "WORKS_AT", "start_label": "Company", "end_label": "Person"},
        )
        result = summarize_candidate(c)
        assert "wrong types" in result

    def test_orphan_node(self):
        result = summarize_candidate(_candidate("orphan_node"))
        assert "Orphan" in result
        assert "no connections" in result

    def test_degree_outlier(self):
        c = _candidate("degree_outlier", {"degree": 45, "average_degree": 5})
        result = summarize_candidate(c)
        assert "45" in result
        assert "5" in result

    def test_missing_provenance_node(self):
        c = _candidate(
            "missing_provenance_node",
            {"node_type": "Person", "missing_field": "schema_version"},
        )
        result = summarize_candidate(c)
        assert "Person" in result
        assert "schema_version" in result

    def test_missing_provenance_rel(self):
        c = _candidate(
            "missing_provenance_rel",
            {"rel_type": "WORKS_AT", "missing_field": "run_id"},
        )
        result = summarize_candidate(c)
        assert "WORKS_AT" in result
        assert "run_id" in result

    def test_qualifier_missing(self):
        c = _candidate(
            "qualifier_missing",
            {"node_type": "Person", "qualifier": "birth_date"},
        )
        result = summarize_candidate(c)
        assert "Person" in result
        assert "birth_date" in result

    def test_duplicate_chain_group(self):
        c = _candidate("duplicate_chain_group", {"chain_length": 5})
        result = summarize_candidate(c)
        assert "5" in result
        assert "chain" in result.lower()

    def test_unknown_method_fallback(self):
        c = _candidate("some_future_detector")
        result = summarize_candidate(c)
        assert "Some Future Detector" in result

    def test_missing_keys(self):
        """Should never crash on missing fields."""
        result = summarize_candidate({})
        assert isinstance(result, str)

    def test_empty_context(self):
        c = {"detection_method": "exact_node_dedupe_key", "collision_context": {}}
        result = summarize_candidate(c)
        assert isinstance(result, str)

    def test_deterministic(self):
        c = _candidate("orphan_node")
        assert summarize_candidate(c) == summarize_candidate(c)


# ---------------------------------------------------------------------------
# short_summary
# ---------------------------------------------------------------------------


class TestShortSummary:
    def test_under_60_chars(self):
        for method in [
            "exact_node_dedupe_key",
            "exact_rel_dedupe_key",
            "orphan_node",
            "degree_outlier",
            "missing_provenance_node",
            "qualifier_missing",
            "duplicate_chain_group",
        ]:
            c = _candidate(method, {"node_type": "Person", "degree": 10, "chain_length": 3})
            result = short_summary(c)
            assert len(result) <= 60, f"{method}: '{result}' is {len(result)} chars"

    def test_fuzzy_with_names(self):
        c = _candidate(
            "jaro_winkler_0.90",
            {"names": ["Alice", "Alyce"]},
        )
        result = short_summary(c)
        assert "Fuzzy" in result
        assert "Alice" in result

    def test_unknown_method(self):
        c = _candidate("brand_new_thing")
        result = short_summary(c)
        assert isinstance(result, str)
        assert len(result) <= 60

    def test_missing_keys(self):
        result = short_summary({})
        assert isinstance(result, str)
