"""tests/unit/test_raw_text_parser.py — Unit tests for _parse_with_raw_text()
text-format branch.

Tests the new ``"text"`` file type handling in the raw-text fallback parser.
Calls the real function — no mocks needed for the text branch.
"""

from __future__ import annotations

import pytest

from api.services.ingestion_parsers import _parse_with_raw_text


# ---------------------------------------------------------------------------
# Successful text parsing
# ---------------------------------------------------------------------------


class TestTextParsing:
    """Valid text files are decoded and returned."""

    def test_utf8_txt_file(self) -> None:
        content = "Hello, world!\n\nSecond paragraph."
        result = _parse_with_raw_text(content.encode("utf-8"), "notes.txt")
        assert result == content

    def test_utf8_csv_file(self) -> None:
        content = "name,age\nAlice,30\nBob,25"
        result = _parse_with_raw_text(content.encode("utf-8"), "data.csv")
        assert result == content

    def test_utf8_html_file(self) -> None:
        content = "<html><body><p>Hello</p></body></html>"
        result = _parse_with_raw_text(content.encode("utf-8"), "page.html")
        assert result == content

    def test_utf8_markdown_file(self) -> None:
        content = "# Title\n\nSome text with **bold**."
        result = _parse_with_raw_text(content.encode("utf-8"), "readme.md")
        assert result == content

    def test_utf8_json_file(self) -> None:
        content = '{"key": "value", "number": 42}'
        result = _parse_with_raw_text(content.encode("utf-8"), "config.json")
        assert result == content

    def test_utf8_rst_file(self) -> None:
        content = "Title\n=====\n\nSome reStructuredText."
        result = _parse_with_raw_text(content.encode("utf-8"), "docs.rst")
        assert result == content

    def test_utf8_xml_file(self) -> None:
        content = '<?xml version="1.0"?><root><item>test</item></root>'
        result = _parse_with_raw_text(content.encode("utf-8"), "data.xml")
        assert result == content

    def test_utf8_tsv_file(self) -> None:
        content = "name\tage\nAlice\t30"
        result = _parse_with_raw_text(content.encode("utf-8"), "data.tsv")
        assert result == content

    def test_utf8_log_file(self) -> None:
        content = "2024-01-01 INFO Started\n2024-01-01 INFO Done"
        result = _parse_with_raw_text(content.encode("utf-8"), "app.log")
        assert result == content

    def test_htm_extension(self) -> None:
        content = "<p>Short page</p>"
        result = _parse_with_raw_text(content.encode("utf-8"), "page.htm")
        assert result == content


# ---------------------------------------------------------------------------
# Latin-1 fallback
# ---------------------------------------------------------------------------


class TestLatin1Fallback:
    """Non-UTF-8 bytes fall back to Latin-1 decoding."""

    def test_latin1_fallback(self) -> None:
        # \xe9 is 'é' in Latin-1 but invalid as a standalone UTF-8 byte
        content_bytes = b"Caf\xe9 au lait"
        result = _parse_with_raw_text(content_bytes, "menu.txt")
        assert "Caf" in result
        assert "lait" in result


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestTextParsingErrors:
    """Error cases for the text branch."""

    def test_empty_text_file_raises(self) -> None:
        with pytest.raises(ValueError, match="empty output"):
            _parse_with_raw_text(b"", "empty.txt")

    def test_whitespace_only_text_file_raises(self) -> None:
        with pytest.raises(ValueError, match="empty output"):
            _parse_with_raw_text(b"   \n\n  \t  ", "blank.txt")

    def test_unsupported_pptx_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported file type"):
            _parse_with_raw_text(b"dummy content", "slides.pptx")

    def test_unsupported_png_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported file type"):
            _parse_with_raw_text(b"\x89PNG\r\n\x1a\n", "image.png")

    def test_unsupported_xlsx_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported file type"):
            _parse_with_raw_text(b"dummy", "data.xlsx")

    def test_unsupported_epub_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported file type"):
            _parse_with_raw_text(b"dummy", "book.epub")

    def test_error_message_mentions_text(self) -> None:
        """Error message for unsupported formats mentions text-based files."""
        with pytest.raises(ValueError, match="text-based file"):
            _parse_with_raw_text(b"dummy", "file.pptx")
