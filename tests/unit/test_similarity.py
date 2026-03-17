"""
tests/unit/test_similarity.py — Unit tests for token containment guard.

No network, no LLM, no Neo4j.
"""

from __future__ import annotations

from api.services.curation.similarity import _token_containment


class TestTokenContainmentSingleTokenGuard:
    """Single-token shorter strings are rejected to avoid first-name-in-full-name FPs."""

    def test_single_token_containment_rejected(self) -> None:
        """'tom' / 'tom hanks' → False (single-token shorter string)."""
        assert _token_containment("tom", "tom hanks") is False

    def test_single_token_containment_rejected_sophia(self) -> None:
        """'sophia' / 'sophia van dijk' → False."""
        assert _token_containment("sophia", "sophia van dijk") is False

    def test_single_token_containment_rejected_ingrid(self) -> None:
        """'ingrid' / 'ingrid bos' → False."""
        assert _token_containment("ingrid", "ingrid bos") is False

    def test_multi_token_containment_accepted(self) -> None:
        """'ingrid bos' / 'ingrid bos van dijk' → True (multi-token subset)."""
        assert _token_containment("ingrid bos", "ingrid bos van dijk") is True

    def test_multi_token_containment_accepted_case_insensitive(self) -> None:
        """Case-insensitive multi-token containment works."""
        assert _token_containment("Ingrid Bos", "Ingrid Bos Van Dijk") is True

    def test_equal_tokens_still_rejected(self) -> None:
        """Equal token sets are handled elsewhere, not by containment."""
        assert _token_containment("ingrid bos", "ingrid bos") is False

    def test_short_string_guard_still_works(self) -> None:
        """Very short strings (< min_short_len chars) are still rejected."""
        assert _token_containment("ab", "ab cd") is False
