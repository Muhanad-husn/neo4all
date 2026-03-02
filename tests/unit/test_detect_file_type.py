"""tests/unit/test_detect_file_type.py — Unit tests for _detect_file_type().

Tests the extension-based and magic-byte file type detection logic in
api/services/ingestion_parsers.py.  Pure function — no mocks needed.
"""

from __future__ import annotations

import pytest

from api.services.ingestion_parsers import _detect_file_type


# ---------------------------------------------------------------------------
# Extension-based detection
# ---------------------------------------------------------------------------


class TestExtensionDetection:
    """Extension-based fast path returns the correct file type."""

    def test_pdf_extension(self) -> None:
        assert _detect_file_type("report.pdf", b"dummy") == "pdf"

    def test_pdf_extension_uppercase(self) -> None:
        assert _detect_file_type("report.PDF", b"dummy") == "pdf"

    def test_docx_extension(self) -> None:
        assert _detect_file_type("report.docx", b"dummy") == "docx"

    def test_docx_extension_uppercase(self) -> None:
        assert _detect_file_type("REPORT.DOCX", b"dummy") == "docx"

    @pytest.mark.parametrize("ext", [".txt", ".md", ".rst", ".csv", ".tsv",
                                      ".html", ".htm", ".xml", ".json", ".log"])
    def test_text_extensions(self, ext: str) -> None:
        assert _detect_file_type(f"file{ext}", b"dummy") == "text"

    def test_text_extension_uppercase(self) -> None:
        assert _detect_file_type("notes.TXT", b"dummy") == "text"

    @pytest.mark.parametrize("ext", [".pptx", ".xlsx", ".odt", ".epub"])
    def test_zip_office_extensions_return_unknown(self, ext: str) -> None:
        assert _detect_file_type(f"file{ext}", b"dummy") == "unknown"

    @pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".bmp", ".tiff"])
    def test_image_extensions_return_unknown(self, ext: str) -> None:
        assert _detect_file_type(f"image{ext}", b"dummy") == "unknown"

    def test_rtf_extension_returns_unknown(self) -> None:
        assert _detect_file_type("doc.rtf", b"dummy") == "unknown"

    def test_unknown_extension(self) -> None:
        assert _detect_file_type("file.xyz", b"dummy") == "unknown"


# ---------------------------------------------------------------------------
# Magic-byte fallback (no extension)
# ---------------------------------------------------------------------------


class TestMagicByteDetection:
    """Magic-byte fallback when no file extension is present."""

    def test_pdf_magic_bytes(self) -> None:
        assert _detect_file_type("noext", b"%PDF-1.7 rest of content") == "pdf"

    def test_zip_magic_bytes_returns_docx(self) -> None:
        assert _detect_file_type("noext", b"PK\x03\x04 rest") == "docx"

    def test_utf8_content_returns_text(self) -> None:
        content = "Hello, world!\nThis is plain text.".encode("utf-8")
        assert _detect_file_type("noext", content) == "text"

    def test_binary_content_returns_unknown(self) -> None:
        # Invalid UTF-8 sequence
        content = b"\x80\x81\x82\x83\xff\xfe\xfd"
        assert _detect_file_type("noext", content) == "unknown"

    def test_empty_bytes_returns_text(self) -> None:
        # Empty bytes decode as valid UTF-8
        assert _detect_file_type("noext", b"") == "text"

    def test_utf8_with_bom_returns_text(self) -> None:
        content = b"\xef\xbb\xbf" + "Hello".encode("utf-8")
        assert _detect_file_type("noext", content) == "text"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for file type detection."""

    def test_extension_takes_priority_over_magic_bytes(self) -> None:
        """A .txt file with PDF magic bytes is still detected as text."""
        assert _detect_file_type("file.txt", b"%PDF-1.7") == "text"

    def test_dotfile_no_extension(self) -> None:
        """A dotfile like '.gitignore' has no suffix, so magic bytes apply.

        Path('.gitignore').suffix returns '' — the entire name is the stem.
        Content is valid UTF-8, so the heuristic returns 'text'.
        """
        result = _detect_file_type(".gitignore", b"node_modules/\n")
        assert result == "text"

    def test_path_with_directory(self) -> None:
        """Source identity with directory separators still detects extension."""
        assert _detect_file_type("uploads/2024/report.csv", b"a,b,c") == "text"
