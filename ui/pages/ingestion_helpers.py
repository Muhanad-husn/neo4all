"""
ui/pages/ingestion_helpers.py — Helper/formatter functions for the ingestion UI.

Extracted from ui/pages/ingestion.py (SKILL-B R-B7 refactor). Contains data
formatting helpers, display helpers, the ingest action handler, the
single-document chunk renderer, and shared constants / API helpers used by
both this module and ingestion.py.

ingestion.py imports shared constants and API helpers from this module.
ingestion.py's main() and section renderers call helper functions defined here.
No circular imports — the dependency is one-directional: ingestion.py -> this file.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pandas as pd
import streamlit as st

from ui.state import StateManager

# ---------------------------------------------------------------------------
# Configuration (shared with ingestion.py)
# ---------------------------------------------------------------------------

_API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8000")
_REQUEST_TIMEOUT_FAST: float = 5.0
_REQUEST_TIMEOUT_SLOW: float = 120.0  # document parsing via Docling can be slow

_ACCEPTED_EXTENSIONS: list[str] = [
    "pdf", "docx", "pptx", "xlsx", "csv", "tsv",
    "html", "htm", "txt", "md", "rst", "rtf",
    "odt", "msg", "eml", "epub",
    "png", "jpg", "jpeg", "tiff", "bmp",
]

# Quality flag display — presentation constants only, not business rules.
_FLAG_LABELS: dict[str, str] = {
    "raw_fallback": "Raw fallback",
    "low_ocr_confidence": "Low OCR confidence",
    "low_text_density": "Low text density",
}
# Row background colors applied by _style_chunk_df — never used for logic.
_FLAG_BG_RAW_FALLBACK: str = "#ffcccc"   # red tint
_FLAG_BG_QUALITY_WARN: str = "#fff3cd"  # yellow tint


# ---------------------------------------------------------------------------
# API helpers (shared with ingestion.py)
# ---------------------------------------------------------------------------


def _post_ingest(
    run_id: str,
    source_identity: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
) -> dict[str, Any] | None:
    """POST multipart document to POST /api/documents/ingest.

    Sends binary file bytes with run_id and source_identity as Form fields,
    matching the FastAPI handler's Form(...) + UploadFile signature.

    Returns the parsed JSON body for both success and HTTP-error responses.
    Returns None only on network / timeout failure so callers can distinguish
    a connectivity failure from a structured API error.

    Args:
        run_id:          Governed run identifier.
        source_identity: Stable source name — typically the original filename.
        file_bytes:      Raw document bytes read via uploaded_file.getvalue().
        filename:        Original filename used as the multipart part name.
        content_type:    MIME type supplied by the Streamlit uploader widget.
    """
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_SLOW) as client:
            response = client.post(
                f"{_API_BASE_URL}/api/documents/ingest",
                data={"run_id": run_id, "source_identity": source_identity},
                files={"file": (filename, file_bytes, content_type)},
            )
            return response.json()  # type: ignore[no-any-return]
    except Exception:
        return None


def _delete_document(run_id: str, doc_id: str) -> dict[str, Any] | None:
    """Send DELETE to /api/documents/{run_id}/{doc_id}.

    Returns parsed JSON on any HTTP response, None on network failure.
    """
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_FAST) as client:
            response = client.delete(
                f"{_API_BASE_URL}/api/documents/{run_id}/{doc_id}",
            )
            return response.json()  # type: ignore[no-any-return]
    except Exception:
        return None


def _get(path: str) -> dict[str, Any] | None:
    """GET from the backend API and return parsed JSON on success, else None.

    Args:
        path: URL path appended to _API_BASE_URL (must start with "/").
    """
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_FAST) as client:
            response = client.get(f"{_API_BASE_URL}{path}")
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Data formatting helpers — UI presentation only
# ---------------------------------------------------------------------------


def _fmt_flags(flags: list[str]) -> str:
    """Format a list of quality flag enum strings as human-readable labels.

    Returns an empty string when the list is empty (unflagged chunk).
    Unknown flag values are passed through unchanged.
    """
    if not flags:
        return ""
    return ", ".join(_FLAG_LABELS.get(f, f) for f in flags)


def _fmt_file_size(byte_count: int) -> str:
    """Format a byte count as a compact human-readable size string."""
    if byte_count < 1024:
        return f"{byte_count} B"
    if byte_count < 1024 * 1024:
        return f"{byte_count / 1024:.1f} KB"
    return f"{byte_count / (1024 * 1024):.1f} MB"


def _docs_to_df(documents: list[dict[str, Any]]) -> pd.DataFrame:
    """Convert GET /api/documents/{run_id} document list to a display DataFrame.

    DocumentSummary exposes doc_id, chunk_count, and created_at only —
    source_identity is not available from the listing endpoint. Doc IDs are
    truncated to 16 characters for readability.
    """
    if not documents:
        return pd.DataFrame(columns=["Doc ID", "Chunks", "Ingested At"])
    return pd.DataFrame(
        [
            {
                "Doc ID": (doc.get("doc_id") or "")[:16] + "\u2026",
                "Chunks": doc.get("chunk_count", 0),
                "Ingested At": doc.get("created_at", ""),
            }
            for doc in documents
        ]
    )


def _chunks_to_df(chunks: list[dict[str, Any]]) -> pd.DataFrame:
    """Convert GET \u2026/chunks chunk list to a display DataFrame.

    chunk.text is excluded from the API response (SKILL-D R-D5); char_count
    is shown instead so the user can gauge chunk size without reading raw text.
    Chunk IDs are truncated to 16 characters.
    """
    if not chunks:
        return pd.DataFrame(
            columns=["Chunk ID", "Start Page", "End Page", "Chars", "Quality Flags"]
        )
    return pd.DataFrame(
        [
            {
                "Chunk ID": chunk.get("chunk_id") or "",
                "Start Page": chunk.get("start_page", 0),
                "End Page": chunk.get("end_page", 0),
                "Chars": chunk.get("char_count", 0),
                "Quality Flags": _fmt_flags(chunk.get("quality_flags") or []),
            }
            for chunk in chunks
        ]
    )


def _style_chunk_df(df: pd.DataFrame) -> pd.io.formats.style.Styler:
    """Apply row background colors to a chunk DataFrame based on quality flags.

    raw_fallback rows -> red tint.
    OCR / density flagged rows -> yellow tint.
    Unflagged rows -> unstyled.

    This is display-only styling — no data is mutated.
    """

    def _row_style(row: pd.Series) -> list[str]:
        flags_str: str = str(row.get("Quality Flags", ""))
        if _FLAG_LABELS["raw_fallback"] in flags_str:
            bg = _FLAG_BG_RAW_FALLBACK
        elif flags_str:
            bg = _FLAG_BG_QUALITY_WARN
        else:
            bg = ""
        return [f"background-color: {bg}" if bg else "" for _ in row]

    return df.style.apply(_row_style, axis=1)


# ---------------------------------------------------------------------------
# Shared display helpers
# ---------------------------------------------------------------------------


def _show_api_errors(result: dict[str, Any]) -> None:
    """Display structured error detail from a BaseResponse payload.

    Renders one st.error per ErrorDetail entry. Falls back to a generic
    message if the errors list is empty.
    """
    errors: list[dict[str, Any]] = result.get("errors", [])
    if not errors:
        st.error("The API returned an error with no detail. Check backend logs.")
        return
    for err in errors:
        code: str = err.get("code", "error")
        message: str = err.get("message", "An unknown error occurred.")
        field: str | None = err.get("field")
        label = f"[{code}] {field}: {message}" if field else f"[{code}] {message}"
        st.error(label)


def _render_quality_summary_metrics(qs: dict[str, Any]) -> None:
    """Render per-flag chunk counts as a three-column st.metric row.

    Works with both QualitySummary (ingest response) and QualityFlagsSummary
    (chunks response) because both share the same per-flag field names.
    Pure presentation — reads pre-aggregated counts from the API response.

    Args:
        qs: Dict with keys raw_fallback, low_ocr_confidence, low_text_density.
    """
    cols = st.columns(3)
    with cols[0]:
        st.metric("Raw fallback", qs.get("raw_fallback", 0))
    with cols[1]:
        st.metric("Low OCR confidence", qs.get("low_ocr_confidence", 0))
    with cols[2]:
        st.metric("Low text density", qs.get("low_text_density", 0))


# ---------------------------------------------------------------------------
# Action handler
# ---------------------------------------------------------------------------


def _handle_ingest(
    state: StateManager,
    uploaded_file: Any,  # st.runtime.uploaded_file_manager.UploadedFile
) -> None:
    """Handle the Ingest Document button click.

    Reads the uploaded file bytes and sends them via multipart POST to
    /api/documents/ingest. Displays the structured result inline. All
    parsing, chunking, and Qdrant indexing logic lives on the backend —
    this function only constructs the HTTP request and renders the response
    (SKILL-B).

    The calling render cycle continues after this function returns, so the
    document list below will reflect the new ingestion within the same
    Streamlit script run — no explicit st.rerun() is required here.

    Args:
        state:         Live StateManager instance.
        uploaded_file: File object returned by st.file_uploader.
    """
    run_id = state.run_id
    if not run_id:
        st.error("No active session. Return to Phase 0 to initialize a session.")
        return

    filename: str = uploaded_file.name
    content_type: str = uploaded_file.type or "application/octet-stream"
    file_bytes: bytes = uploaded_file.getvalue()

    if not file_bytes:
        st.error(
            f"The file '{filename}' appears to be empty. "
            "Please select a non-empty document."
        )
        return

    with st.spinner(
        f"Ingesting '{filename}' \u2014 parsing and chunking may take 10\u201360 seconds\u2026"
    ):
        result = _post_ingest(
            run_id=run_id,
            source_identity=filename,
            file_bytes=file_bytes,
            filename=filename,
            content_type=content_type,
        )

    if result is None:
        st.error(
            "Cannot reach the backend API. "
            "Ensure the API server is running and retry."
        )
        return

    status: str = result.get("status", "error")

    if status == "error":
        _show_api_errors(result)
        return

    doc_id: str = result.get("doc_id", "")
    chunk_count: int = result.get("chunk_count", 0)
    parser_used: str = result.get("parser_used", "unknown")
    chunk_noun = "chunk" if chunk_count == 1 else "chunks"

    if status == "partial":
        st.warning(
            f"Document ingested with warnings \u2014 "
            f"{chunk_count} {chunk_noun} produced using **{parser_used}** parser. "
            f"Manifest storage failed; this document will be re-parsed on the next upload.",
        )
        _show_api_errors(result)
    else:
        st.success(
            f"Document ingested \u2014 {chunk_count} {chunk_noun} produced "
            f"using **{parser_used}** parser.  `doc_id: {doc_id[:16]}\u2026`"
        )

    # Show quality flag summary when flags were detected during ingestion.
    qs: dict[str, Any] | None = result.get("quality_summary")
    if qs and qs.get("flagged_chunks", 0) > 0:
        flagged: int = qs["flagged_chunks"]
        total: int = qs.get("total_chunks", chunk_count)
        st.info(
            f"{flagged} of {total} {chunk_noun} carry quality flags \u2014 "
            "see the chunk manifest viewer below for per-chunk detail.",
            icon="\u26a0",
        )
        _render_quality_summary_metrics(qs)


# ---------------------------------------------------------------------------
# Sub-renderer: single document chunk viewer
# ---------------------------------------------------------------------------


def _render_single_doc_chunks(
    run_id: str,
    doc_id: str,
    phase: Any = None,
) -> None:
    """Fetch and render chunk metadata for a single document.

    Calls GET /api/documents/{run_id}/{doc_id}/chunks. Renders a quality flag
    summary metric row (when flags are present) followed by a styled chunk
    DataFrame. Uses pandas Styler to apply row-level background colors
    without modifying any data.

    When phase is Phase.INGESTION, a "Delete & Re-upload" button is shown
    after the chunk table so the user can remove this document and try again.

    Args:
        run_id: Governed run identifier.
        doc_id: Full 64-character doc_id of the target document.
        phase:  Current Phase value; delete button shown only during INGESTION.
    """
    with st.spinner("Loading chunks\u2026"):
        data = _get(f"/api/documents/{run_id}/{doc_id}/chunks")

    if data is None:
        st.error("Could not fetch chunks. Check that the API server is running.")
        return

    if data.get("status") == "error":
        _show_api_errors(data)
        return

    chunks: list[dict[str, Any]] = data.get("chunks") or []
    flags_summary: dict[str, Any] = data.get("quality_flags_summary") or {}

    if not chunks:
        st.info("No chunks found for this document.")
        return

    # Render quality metrics row only when at least one flag type is present.
    has_flags = any(
        flags_summary.get(k, 0) > 0
        for k in ("raw_fallback", "low_ocr_confidence", "low_text_density")
    )
    if has_flags:
        _render_quality_summary_metrics(flags_summary)

    chunk_df = _chunks_to_df(chunks)
    styled = _style_chunk_df(chunk_df)

    st.dataframe(
        styled,
        width="stretch",
        hide_index=True,
        column_config={
            "Chunk ID": st.column_config.TextColumn(
                "Chunk ID (prefix)",
                width="medium",
                help="First 16 characters of the 64-character SHA-256 chunk_id.",
            ),
            "Start Page": st.column_config.NumberColumn(
                "Start Page",
                width="small",
                help="0-indexed page where the chunk begins.",
            ),
            "End Page": st.column_config.NumberColumn(
                "End Page",
                width="small",
                help="0-indexed page where the chunk ends.",
            ),
            "Chars": st.column_config.NumberColumn(
                "Chars",
                width="small",
                help="Character count of the chunk text (text itself is not displayed).",
            ),
            "Quality Flags": st.column_config.TextColumn(
                "Quality Flags",
                width="large",
                help=(
                    "Active quality signals: raw_fallback, low_ocr_confidence, "
                    "low_text_density."
                ),
            ),
        },
    )

    # --- Chunk text lookup (inline) ---
    with st.expander("Look up chunk text", expanded=False):
        lookup_key = f"chunk_lookup_{doc_id[:16]}"
        lookup_input = st.text_input(
            "Paste a full Chunk ID to view its text",
            key=lookup_key,
            placeholder="64-character hex chunk ID\u2026",
            label_visibility="collapsed",
        )
        if lookup_input:
            cid_clean = lookup_input.strip()
            if cid_clean:
                lookup_data = _get(
                    f"/api/documents/{run_id}/chunk/{cid_clean}/text"
                )
                if lookup_data is None:
                    st.error("Could not reach the API.")
                elif lookup_data.get("status") == "error":
                    for e in lookup_data.get("errors", []):
                        st.error(
                            f"[{e.get('code', 'error')}] {e.get('message', '')}"
                        )
                else:
                    text = lookup_data.get("text", "")
                    if text:
                        st.success(f"{len(text)} characters")
                        st.text_area(
                            "Chunk text",
                            value=text,
                            height=200,
                            disabled=True,
                            key=f"chunk_text_{cid_clean[:16]}",
                        )
                    else:
                        st.warning("Chunk not found or has no text.")

    # --- Delete button (only during Ingestion phase) ---
    from api.models.run import Phase

    if phase == Phase.INGESTION:
        btn_key = f"del_{doc_id[:16]}"

        with st.popover("Delete & Re-upload", help="Remove this document so you can re-upload a corrected version."):
            st.warning(
                "This will delete the document's manifest, chunks, and "
                "job statuses. Raw bytes are retained for audit."
            )
            if st.button("Confirm delete", key=btn_key, type="primary"):
                with st.spinner("Deleting document\u2026"):
                    result = _delete_document(run_id, doc_id)
                if result is None:
                    st.error("Cannot reach the backend API.")
                elif result.get("status") == "error":
                    _show_api_errors(result)
                else:
                    cd = result.get("chunks_deleted", 0)
                    jc = result.get("jobs_cleared", 0)
                    st.success(
                        f"Document deleted \u2014 {cd} chunks removed, "
                        f"{jc} job statuses cleared."
                    )
                    st.rerun()
