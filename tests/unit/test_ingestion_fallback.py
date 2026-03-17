"""
tests/unit/test_ingestion_fallback.py — IngestorService parser fallback chain
(SPEC-03 file 13).

Acceptance criteria (CLAUDE.md §4.4, SPEC-03 S-03.1):
  - Docling success: parser_used='docling', no quality flags, others not called.
  - Docling failure: Unstructured activates; 'fallback_triggered' WARNING logged.
  - Both structural parsers fail: raw_text tier activates; every chunk must carry
    ChunkQualityFlag.raw_fallback (set on IngestResult.default_quality_flags).
  - All enabled tiers fail: IngestionError raised with code='all_parsers_failed'.
  - Each fallback transition emits structured WARNING events ('parser_failed' and
    'fallback_triggered') via the centralized logger (SKILL-D R-D3).
  - Parser tiers disabled via Settings toggles are silently skipped; all-disabled
    raises IngestionError with code='no_parsers_enabled'.

IngestorService is tested via dependency injection (settings + artifacts_service).
No real parsers, no network, no file I/O.  All parser functions are patched at
the module level (api.services.ingestion._parse_with_*).
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from api.models.document import ChunkQualityFlag
from api.services.ingestion import IngestionError, IngestorService

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-ingestion-00001"
_SOURCE_IDENTITY = "test_document.pdf"

# Non-empty bytes that IngestorService hashes for content_hash / doc_id.
# No actual parsing occurs — parser functions are mocked.
_FILE_BYTES = b"Unit test document content for ingestion fallback tests."

# Canonical text each mock parser tier returns on success.
_DOCLING_TEXT = "Docling structural extraction output."
_UNSTRUCTURED_TEXT = "Unstructured extraction output."
_RAW_TEXT = "Raw text extraction output."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_settings(
    *,
    docling: bool = True,
    unstructured: bool = True,
    raw: bool = True,
) -> types.SimpleNamespace:
    """Minimal settings stub with only the parser toggle fields.

    Injected into IngestorService to avoid calling get_settings(), which
    requires all required environment variables to be set.
    """
    return types.SimpleNamespace(
        ENABLE_DOCLING=docling,
        ENABLE_UNSTRUCTURED=unstructured,
        ENABLE_RAW_FALLBACK=raw,
    )


def _make_service(settings: types.SimpleNamespace) -> IngestorService:
    """Construct IngestorService with injected settings and no ArtifactsService.

    artifacts_service=None skips the manifest cache check so tests exercise
    only the parser fallback chain.
    """
    return IngestorService(settings=settings, artifacts_service=None)


# ---------------------------------------------------------------------------
# Tests: successful parser tiers
# ---------------------------------------------------------------------------


async def test_docling_success() -> None:
    """Docling returns text → parser_used='docling', no quality flags, no fallback.

    When Docling succeeds the cascade stops immediately.  Unstructured and
    raw_text are never called.  IngestResult carries no default_quality_flags.
    """
    service = _make_service(_fake_settings())

    mock_unstructured = MagicMock(return_value=_UNSTRUCTURED_TEXT)
    mock_raw = MagicMock(return_value=_RAW_TEXT)

    with (
        patch("api.services.ingestion._parse_with_docling", return_value=_DOCLING_TEXT),
        patch("api.services.ingestion._parse_with_unstructured", mock_unstructured),
        patch("api.services.ingestion._parse_with_raw_text", mock_raw),
    ):
        result = await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    assert result.document.parser_used == "docling"
    assert result.extracted_text == _DOCLING_TEXT
    assert result.default_quality_flags == ()
    assert result.existing_manifest is None

    # Downstream tiers must not have been called
    mock_unstructured.assert_not_called()
    mock_raw.assert_not_called()


async def test_docling_fails_unstructured_succeeds() -> None:
    """Docling failure → Unstructured is activated as the second tier.

    parser_used='unstructured'; no quality flags are added (raw_fallback is
    only set when the raw_text tier wins).
    """
    service = _make_service(_fake_settings())

    with (
        patch(
            "api.services.ingestion._parse_with_docling",
            side_effect=RuntimeError("docling conversion error"),
        ),
        patch(
            "api.services.ingestion._parse_with_unstructured",
            return_value=_UNSTRUCTURED_TEXT,
        ),
    ):
        result = await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    assert result.document.parser_used == "unstructured"
    assert result.extracted_text == _UNSTRUCTURED_TEXT
    assert result.default_quality_flags == ()


async def test_both_fail_raw_fallback() -> None:
    """Both Docling and Unstructured fail → raw_text tier activates.

    parser_used='raw_text'.  IngestResult.default_quality_flags must contain
    ChunkQualityFlag.raw_fallback so ChunkingService stamps it on every chunk.
    """
    service = _make_service(_fake_settings())

    with (
        patch(
            "api.services.ingestion._parse_with_docling",
            side_effect=RuntimeError("docling failed"),
        ),
        patch(
            "api.services.ingestion._parse_with_unstructured",
            side_effect=RuntimeError("unstructured failed"),
        ),
        patch(
            "api.services.ingestion._parse_with_raw_text",
            return_value=_RAW_TEXT,
        ),
    ):
        result = await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    assert result.document.parser_used == "raw_text"
    assert result.extracted_text == _RAW_TEXT
    assert ChunkQualityFlag.raw_fallback in result.default_quality_flags


# ---------------------------------------------------------------------------
# Tests: hard-reject
# ---------------------------------------------------------------------------


async def test_all_fail_hard_reject() -> None:
    """All three tiers fail → IngestionError with code='all_parsers_failed'.

    The system never silently accepts an empty result (CLAUDE.md §4.4 fail-closed).
    The error must name the parser chain length and preserve the last error reason.
    """
    service = _make_service(_fake_settings())

    with (
        patch(
            "api.services.ingestion._parse_with_docling",
            side_effect=RuntimeError("docling crashed"),
        ),
        patch(
            "api.services.ingestion._parse_with_unstructured",
            side_effect=RuntimeError("unstructured crashed"),
        ),
        patch(
            "api.services.ingestion._parse_with_raw_text",
            side_effect=RuntimeError("pypdf2 crashed"),
        ),
    ):
        with pytest.raises(IngestionError) as exc_info:
            await service.ingest_document(
                run_id=_RUN_ID,
                file_bytes=_FILE_BYTES,
                source_identity=_SOURCE_IDENTITY,
            )

    assert exc_info.value.code == "all_parsers_failed"
    # Error message should preserve the last error for diagnostics
    assert "pypdf2 crashed" in exc_info.value.message


# ---------------------------------------------------------------------------
# Tests: structured logging of fallback transitions
# ---------------------------------------------------------------------------


async def test_fallback_logging() -> None:
    """Fallback transitions are logged as structured WARNING events.

    When Docling fails and Unstructured takes over the service must emit:
      1. 'parser_failed'     WARNING — for the Docling failure
      2. 'fallback_triggered' WARNING — announcing the transition to Unstructured

    Event names are the first positional argument to logger.warning() per the
    SKILL-D R-D3 structured-event convention.
    """
    service = _make_service(_fake_settings())

    with (
        patch(
            "api.services.ingestion._parse_with_docling",
            side_effect=RuntimeError("docling crashed"),
        ),
        patch(
            "api.services.ingestion._parse_with_unstructured",
            return_value=_UNSTRUCTURED_TEXT,
        ),
        patch("api.services.ingestion.logger") as mock_logger,
    ):
        await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    warning_events = [c.args[0] for c in mock_logger.warning.call_args_list]

    assert "parser_failed" in warning_events, (
        "Expected 'parser_failed' WARNING when Docling raises an exception"
    )
    assert "fallback_triggered" in warning_events, (
        "Expected 'fallback_triggered' WARNING on the Docling → Unstructured transition"
    )


async def test_fallback_logging_all_three_tiers() -> None:
    """Two fallback transitions each produce 'parser_failed' + 'fallback_triggered'.

    When both Docling and Unstructured fail, the service transitions twice:
      Docling → Unstructured → raw_text

    The 'fallback_triggered' event must appear twice and carry the correct
    from_parser / to_parser fields for each transition.
    """
    service = _make_service(_fake_settings())

    with (
        patch(
            "api.services.ingestion._parse_with_docling",
            side_effect=RuntimeError("docling failed"),
        ),
        patch(
            "api.services.ingestion._parse_with_unstructured",
            side_effect=RuntimeError("unstructured failed"),
        ),
        patch(
            "api.services.ingestion._parse_with_raw_text",
            return_value=_RAW_TEXT,
        ),
        patch("api.services.ingestion.logger") as mock_logger,
    ):
        await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    warning_events = [c.args[0] for c in mock_logger.warning.call_args_list]
    fallback_calls = [
        c for c in mock_logger.warning.call_args_list
        if c.args[0] == "fallback_triggered"
    ]

    assert warning_events.count("fallback_triggered") == 2, (
        "Two fallback transitions should produce two 'fallback_triggered' events"
    )

    # First transition: docling → unstructured
    assert fallback_calls[0].kwargs["from_parser"] == "docling"
    assert fallback_calls[0].kwargs["to_parser"] == "unstructured"

    # Second transition: unstructured → raw_text
    assert fallback_calls[1].kwargs["from_parser"] == "unstructured"
    assert fallback_calls[1].kwargs["to_parser"] == "raw_text"


async def test_successful_parse_logs_ingestion_complete() -> None:
    """ingestion_complete INFO event is logged with doc_id and parser_used on success."""
    service = _make_service(_fake_settings())

    with (
        patch(
            "api.services.ingestion._parse_with_docling",
            return_value=_DOCLING_TEXT,
        ),
        patch("api.services.ingestion.logger") as mock_logger,
    ):
        result = await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    info_events = [c.args[0] for c in mock_logger.info.call_args_list]
    assert "ingestion_complete" in info_events

    complete_call = next(
        c for c in mock_logger.info.call_args_list
        if c.args[0] == "ingestion_complete"
    )
    assert complete_call.kwargs["parser_used"] == "docling"
    assert complete_call.kwargs["doc_id"] == result.document.doc_id


# ---------------------------------------------------------------------------
# Tests: parser toggle configuration
# ---------------------------------------------------------------------------


async def test_parser_config_toggles_docling_disabled() -> None:
    """When ENABLE_DOCLING=False, Docling is not called and Unstructured runs first.

    The disabled tier is completely absent from the cascade — it is not called
    and no fallback events are emitted for the skipped tier.
    """
    service = _make_service(_fake_settings(docling=False, unstructured=True, raw=True))

    mock_docling = MagicMock(return_value=_DOCLING_TEXT)

    with (
        patch("api.services.ingestion._parse_with_docling", mock_docling),
        patch(
            "api.services.ingestion._parse_with_unstructured",
            return_value=_UNSTRUCTURED_TEXT,
        ),
    ):
        result = await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    # Docling function must never have been called
    mock_docling.assert_not_called()
    assert result.document.parser_used == "unstructured"


async def test_parser_config_toggles_all_disabled() -> None:
    """ENABLE_DOCLING=ENABLE_UNSTRUCTURED=ENABLE_RAW_FALLBACK=False → IngestionError.

    The service raises immediately with code='no_parsers_enabled' without
    attempting any parse (there is no tier list to iterate).
    """
    service = _make_service(_fake_settings(docling=False, unstructured=False, raw=False))

    with pytest.raises(IngestionError) as exc_info:
        await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    assert exc_info.value.code == "no_parsers_enabled"


async def test_parser_config_toggles_only_raw_enabled() -> None:
    """When only raw fallback is enabled, it is used as the sole tier.

    No fallback_triggered events are logged (there is only one tier in the
    cascade, so no transition ever occurs).
    """
    service = _make_service(_fake_settings(docling=False, unstructured=False, raw=True))

    with (
        patch(
            "api.services.ingestion._parse_with_raw_text",
            return_value=_RAW_TEXT,
        ),
        patch("api.services.ingestion.logger") as mock_logger,
    ):
        result = await service.ingest_document(
            run_id=_RUN_ID,
            file_bytes=_FILE_BYTES,
            source_identity=_SOURCE_IDENTITY,
        )

    assert result.document.parser_used == "raw_text"
    assert ChunkQualityFlag.raw_fallback in result.default_quality_flags

    # No transitions occurred — fallback_triggered must not have been logged
    warning_events = [c.args[0] for c in mock_logger.warning.call_args_list]
    assert "fallback_triggered" not in warning_events
