"""
tests/unit/test_documents_helpers.py — _classify_element and _text_to_elements tests.

Validates that Markdown structure detection in _text_to_elements correctly
identifies headings, tables, list items, and paragraphs so that
ChunkingService heading-aware logic fires properly.

No network, no LLM, no Neo4j, no file I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from api.models.document import Document, derive_content_hash
from api.routers.documents_helpers import _classify_element, _text_to_elements
from api.services.chunking import ChunkingService, ParsedElementType

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-helpers-00001"
_SOURCE_IDENTITY = "reports/test_document.md"


def _make_document(text: str = "test") -> Document:
    return Document(
        run_id=_RUN_ID,
        source_identity=_SOURCE_IDENTITY,
        content_hash=derive_content_hash(text.encode()),
        parser_used="raw_text",
        ingested_at=datetime(2025, 1, 1, tzinfo=UTC),
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# _classify_element: heading detection
# ---------------------------------------------------------------------------


class TestClassifyHeading:
    """Headings: lines starting with 1-6 '#' followed by space + content."""

    @pytest.mark.parametrize("level", range(1, 7))
    def test_heading_levels_1_through_6(self, level: int) -> None:
        text = "#" * level + " Section Title"
        assert _classify_element(text) == ParsedElementType.heading

    def test_seven_hashes_not_heading(self) -> None:
        assert _classify_element("####### Too many") != ParsedElementType.heading

    def test_no_space_after_hash_not_heading(self) -> None:
        assert _classify_element("#no-space") != ParsedElementType.heading

    def test_hash_only_not_heading(self) -> None:
        assert _classify_element("# ") != ParsedElementType.heading

    def test_heading_with_leading_blank_lines(self) -> None:
        text = "\n\n## Heading After Blanks"
        assert _classify_element(text) == ParsedElementType.heading


# ---------------------------------------------------------------------------
# _classify_element: table detection
# ---------------------------------------------------------------------------


class TestClassifyTable:
    """Tables: contain a separator row like |---|---|."""

    def test_basic_table(self) -> None:
        text = "| Col A | Col B |\n|-------|-------|\n| val1  | val2  |"
        assert _classify_element(text) == ParsedElementType.table

    def test_separator_with_alignment(self) -> None:
        text = "| Left | Center | Right |\n|:-----|:------:|------:|\n| a | b | c |"
        assert _classify_element(text) == ParsedElementType.table

    def test_single_pipe_not_table(self) -> None:
        text = "This has a | pipe but is not a table"
        assert _classify_element(text) != ParsedElementType.table

    def test_separator_without_multiple_columns(self) -> None:
        text = "---"
        assert _classify_element(text) != ParsedElementType.table


# ---------------------------------------------------------------------------
# _classify_element: list item detection
# ---------------------------------------------------------------------------


class TestClassifyListItem:
    """List items: lines starting with -, *, +, or ordered number markers."""

    @pytest.mark.parametrize("marker", ["- ", "* ", "+ "])
    def test_unordered_markers(self, marker: str) -> None:
        assert _classify_element(f"{marker}Item text") == ParsedElementType.list_item

    @pytest.mark.parametrize("marker", ["1. ", "2) ", "10. "])
    def test_ordered_markers(self, marker: str) -> None:
        assert _classify_element(f"{marker}Item text") == ParsedElementType.list_item

    def test_dash_no_space_not_list(self) -> None:
        assert _classify_element("-no-space") != ParsedElementType.list_item

    def test_indented_list(self) -> None:
        assert _classify_element("  - Nested item") == ParsedElementType.list_item


# ---------------------------------------------------------------------------
# _classify_element: paragraph (default)
# ---------------------------------------------------------------------------


class TestClassifyParagraph:
    """Plain text without structural markers → paragraph."""

    def test_plain_text(self) -> None:
        assert _classify_element("Just a regular sentence.") == ParsedElementType.paragraph

    def test_empty_string(self) -> None:
        assert _classify_element("") == ParsedElementType.paragraph

    def test_whitespace_only(self) -> None:
        assert _classify_element("   ") == ParsedElementType.paragraph


# ---------------------------------------------------------------------------
# _text_to_elements: mixed markdown classification
# ---------------------------------------------------------------------------


class TestTextToElements:
    """_text_to_elements splits and classifies blocks correctly."""

    def test_mixed_markdown(self) -> None:
        text = "# Introduction\n\nSome body text here.\n\n- Item one\n- Item two"
        elements = _text_to_elements(text)

        assert len(elements) == 3
        assert elements[0].element_type == ParsedElementType.heading
        assert elements[1].element_type == ParsedElementType.paragraph
        assert elements[2].element_type == ParsedElementType.list_item

    def test_all_elements_have_page_zero(self) -> None:
        elements = _text_to_elements("# Heading\n\nParagraph\n\n- List")
        assert all(e.page == 0 for e in elements)

    def test_plain_text_no_false_positives(self) -> None:
        text = "First paragraph here.\n\nSecond paragraph follows.\n\nThird one too."
        elements = _text_to_elements(text)
        assert len(elements) == 3
        assert all(e.element_type == ParsedElementType.paragraph for e in elements)

    def test_table_block(self) -> None:
        text = "# Data\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        elements = _text_to_elements(text)
        assert elements[0].element_type == ParsedElementType.heading
        assert elements[1].element_type == ParsedElementType.table

    def test_single_block_fallback(self) -> None:
        elements = _text_to_elements("single block no separators")
        assert len(elements) == 1
        assert elements[0].element_type == ParsedElementType.paragraph


# ---------------------------------------------------------------------------
# End-to-end: _text_to_elements → ChunkingService
# ---------------------------------------------------------------------------


class TestEndToEndChunking:
    """Markdown with N sections → _text_to_elements → ChunkingService → N chunks."""

    def test_three_sections_produce_three_chunks(self) -> None:
        md = (
            "# Section One\n\nContent of section one.\n\n"
            "# Section Two\n\nContent of section two.\n\n"
            "# Section Three\n\nContent of section three."
        )
        doc = _make_document(md)
        elements = _text_to_elements(md)
        svc = ChunkingService()
        chunks = svc.chunk_document(doc, elements)

        assert len(chunks) == 3
        assert "Section One" in chunks[0].text
        assert "Section Two" in chunks[1].text
        assert "Section Three" in chunks[2].text

    def test_plain_text_still_works(self) -> None:
        """Regression: plain text without headings still produces chunks."""
        text = "Para one.\n\nPara two.\n\nPara three."
        doc = _make_document(text)
        elements = _text_to_elements(text)
        svc = ChunkingService()
        chunks = svc.chunk_document(doc, elements)

        # All paragraphs, so they accumulate into one chunk (below size limit)
        assert len(chunks) >= 1
        assert "Para one." in chunks[0].text
