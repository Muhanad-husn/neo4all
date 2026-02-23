"""
ui/pages/ingestion.py — Phase 2: Document Ingestion & Chunking (SPEC-03 S-03.7).

Users upload PDF or DOCX documents. Each upload is forwarded to
POST /api/documents/ingest, which runs the three-tier parser chain
(Docling → Unstructured → raw text) and indexes resulting chunks in Qdrant.

The page renders four sequential steps:
  1. Upload panel — st.file_uploader accepting PDF/DOCX + Ingest button.
  2. Document list — read-only st.dataframe of all ingested documents for
     this run, fetched fresh from GET /api/documents/{run_id} each render.
  3. Chunk manifest viewer — per-document expandable chunk metadata table
     with quality-flag color coding:
       red  = raw_fallback   (tertiary parser; no structural metadata)
       yellow = low_ocr_confidence or low_text_density
  4. Proceed button — enabled when ≥1 document ingested; advances to
     Phase 3 (Extraction) via StateManager.advance_phase().

Architecture rules enforced here
----------------------------------
- All st.session_state access via StateManager.get() only (CLAUDE.md §8).
- No business logic — no parsing, chunking, ID derivation, or domain
  validation. All operations delegated to the backend API (SKILL-B).
- API calls via httpx synchronous client (Streamlit is single-threaded).
- No imports from api/graph/, api/agents/, api/vector/, api/diff/, api/audit/.
  Only api.models.run is imported for the Phase enum (SKILL-B R-B3).

Startup behaviour
-----------------
The document list is fetched on every render from the backend. No ingestion
state is persisted in StateManager — re-fetching is cheap and guarantees
consistency after browser refreshes or re-uploads.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pandas as pd
import streamlit as st

from api.models.run import Phase
from ui.state import StateManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8000")
_REQUEST_TIMEOUT_FAST: float = 5.0
_REQUEST_TIMEOUT_SLOW: float = 120.0  # document parsing via Docling can be slow

_ACCEPTED_EXTENSIONS: list[str] = ["pdf", "docx"]

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
# API helpers
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
                "Doc ID": (doc.get("doc_id") or "")[:16] + "…",
                "Chunks": doc.get("chunk_count", 0),
                "Ingested At": doc.get("created_at", ""),
            }
            for doc in documents
        ]
    )


def _chunks_to_df(chunks: list[dict[str, Any]]) -> pd.DataFrame:
    """Convert GET …/chunks chunk list to a display DataFrame.

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
                "Chunk ID": (chunk.get("chunk_id") or "")[:16] + "…",
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

    raw_fallback rows → red tint.
    OCR / density flagged rows → yellow tint.
    Unflagged rows → unstyled.

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
            "Please select a non-empty PDF or DOCX document."
        )
        return

    with st.spinner(
        f"Ingesting '{filename}' — parsing and chunking may take 10–60 seconds…"
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
            f"Document ingested with warnings — "
            f"{chunk_count} {chunk_noun} produced using **{parser_used}** parser. "
            f"Manifest storage failed; this document will be re-parsed on the next upload.",
        )
        _show_api_errors(result)
    else:
        st.success(
            f"Document ingested — {chunk_count} {chunk_noun} produced "
            f"using **{parser_used}** parser.  `doc_id: {doc_id[:16]}…`"
        )

    # Show quality flag summary when flags were detected during ingestion.
    qs: dict[str, Any] | None = result.get("quality_summary")
    if qs and qs.get("flagged_chunks", 0) > 0:
        flagged: int = qs["flagged_chunks"]
        total: int = qs.get("total_chunks", chunk_count)
        st.info(
            f"{flagged} of {total} {chunk_noun} carry quality flags — "
            "see the chunk manifest viewer below for per-chunk detail.",
            icon="⚠",
        )
        _render_quality_summary_metrics(qs)


# ---------------------------------------------------------------------------
# Section: Upload panel
# ---------------------------------------------------------------------------


def _render_upload_section(state: StateManager) -> None:
    """Render the file uploader and Ingest Document button.

    Accepts PDF and DOCX files (enforced by Streamlit's type= filter).
    The Ingest button is disabled until a file is selected. No parsing or
    chunking logic exists here — all domain processing is on the backend
    (SKILL-B). Displays file name and size when a file is selected.

    Args:
        state: Live StateManager instance.
    """
    st.subheader("Step 1: Upload a Document")
    st.write(
        "Select a PDF or DOCX file to ingest. "
        "The backend parses, chunks, and indexes it automatically. "
        "Upload multiple documents one at a time."
    )

    uploaded_file = st.file_uploader(
        "Choose a file",
        type=_ACCEPTED_EXTENSIONS,
        help="Accepted formats: PDF, DOCX.",
        label_visibility="collapsed",
    )

    if uploaded_file is not None:
        file_size = _fmt_file_size(len(uploaded_file.getvalue()))
        st.caption(f"Selected: **{uploaded_file.name}** ({file_size})")

        if st.button("Ingest Document", type="primary"):
            _handle_ingest(state, uploaded_file)
    else:
        # Render a disabled button with explanatory caption when no file is chosen.
        st.button("Ingest Document", type="primary", disabled=True)
        st.caption("Select a PDF or DOCX file above to enable ingestion.")


# ---------------------------------------------------------------------------
# Section: Document list table
# ---------------------------------------------------------------------------


def _render_document_list(run_id: str) -> list[dict[str, Any]]:
    """Fetch and render the document list for this run.

    Calls GET /api/documents/{run_id} on every render — no client-side
    caching. This guarantees the table is current after any ingestion within
    the same Streamlit script run. Returns the raw document list so the chunk
    viewer can reuse it without a second API round-trip.

    Args:
        run_id: Governed run identifier.

    Returns:
        List of document dicts from the API; empty list on any failure.
    """
    st.subheader("Step 2: Ingested Documents")

    data = _get(f"/api/documents/{run_id}")

    if data is None:
        st.warning(
            "Could not reach the backend API to fetch the document list. "
            "Check that the API server is running.",
        )
        return []

    if data.get("status") == "error":
        _show_api_errors(data)
        return []

    documents: list[dict[str, Any]] = data.get("documents") or []
    total: int = data.get("total_count", 0)

    if not documents:
        st.info(
            "No documents ingested yet for this run. "
            "Upload a file above to get started.",
            icon="ℹ",
        )
        return []

    doc_noun = "document" if total == 1 else "documents"
    st.caption(
        f"{total} {doc_noun} ingested in this run. "
        "Doc IDs are truncated to 16 characters for display."
    )

    st.dataframe(
        _docs_to_df(documents),
        use_container_width=True,
        hide_index=True,
        column_config={
            "Doc ID": st.column_config.TextColumn(
                "Doc ID (prefix)",
                width="medium",
                help="First 16 characters of the 64-character SHA-256 doc_id.",
            ),
            "Chunks": st.column_config.NumberColumn(
                "Chunks",
                width="small",
                help="Number of chunks produced from this document.",
            ),
            "Ingested At": st.column_config.DatetimeColumn(
                "Ingested At",
                format="YYYY-MM-DD HH:mm:ss",
                width="medium",
                help="UTC timestamp when the manifest was finalised.",
            ),
        },
    )

    return documents


# ---------------------------------------------------------------------------
# Section: Chunk manifest viewer
# ---------------------------------------------------------------------------


def _render_chunk_viewer(run_id: str, documents: list[dict[str, Any]]) -> None:
    """Render per-document chunk manifests in collapsible expanders.

    Each document is represented by one st.expander. Opening it fetches chunk
    metadata via GET /api/documents/{run_id}/{doc_id}/chunks and renders a
    quality flag summary followed by a color-coded chunk table. Chunk text is
    never displayed — only IDs, page ranges, char counts, and quality flags
    (SKILL-D R-D5).

    Args:
        run_id:    Governed run identifier.
        documents: Document dicts returned by _render_document_list.
    """
    if not documents:
        return

    st.subheader("Step 3: Chunk Manifest Viewer")
    st.caption(
        "Expand a document to inspect its chunk metadata. "
        ":red[Red rows] = raw-fallback parser (no structural metadata). "
        ":orange[Yellow rows] = low OCR confidence or low text density."
    )

    for doc in documents:
        doc_id: str = doc.get("doc_id", "")
        chunk_count: int = doc.get("chunk_count", 0)
        chunk_noun = "chunk" if chunk_count == 1 else "chunks"
        label = f"`{doc_id[:16]}…` — {chunk_count} {chunk_noun}"

        with st.expander(label, expanded=False):
            _render_single_doc_chunks(run_id=run_id, doc_id=doc_id)


def _render_single_doc_chunks(run_id: str, doc_id: str) -> None:
    """Fetch and render chunk metadata for a single document.

    Calls GET /api/documents/{run_id}/{doc_id}/chunks. Renders a quality flag
    summary metric row (when flags are present) followed by a styled chunk
    DataFrame. Uses pandas Styler to apply row-level background colors
    without modifying any data.

    Args:
        run_id: Governed run identifier.
        doc_id: Full 64-character doc_id of the target document.
    """
    with st.spinner("Loading chunks…"):
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
        use_container_width=True,
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


# ---------------------------------------------------------------------------
# Section: Proceed button
# ---------------------------------------------------------------------------


def _render_proceed_section(state: StateManager, *, has_docs: bool) -> None:
    """Render the Proceed to Phase 3 button.

    The button is disabled until at least one document has been successfully
    ingested (i.e. has_docs is True). On click, advances the phase to
    Phase.EXTRACTION via StateManager.advance_phase() — the only permitted
    write path for phase transitions (CLAUDE.md §8).

    Args:
        state:    Live StateManager instance.
        has_docs: True when ≥1 document appears in the backend listing.
    """
    st.divider()

    if not has_docs:
        st.button(
            "Proceed to Phase 3: AI-Assisted Extraction →",
            type="primary",
            disabled=True,
        )
        st.caption("Ingest at least one document to enable this button.")
        return

    if st.button(
        "Proceed to Phase 3: AI-Assisted Extraction →",
        type="primary",
    ):
        try:
            state.advance_phase(Phase.EXTRACTION)
        except ValueError as exc:
            st.error(f"Phase transition error: {exc}")
            return
        st.rerun()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Phase 2 entry point. Called by ui/app.py when phase == Phase.INGESTION."""
    # All session state access flows through StateManager (CLAUDE.md §8).
    state = StateManager.get()

    st.title("Phase 2 — Document Ingestion & Chunking")
    st.write(
        "Upload your source documents. "
        "Each file is parsed using a three-tier chain "
        "(Docling → Unstructured → raw text), chunked semantically, "
        "and indexed for AI-assisted extraction in Phase 3."
    )

    run_id = state.run_id
    if not run_id:
        st.error(
            "No active session. "
            "Return to Phase 0 to initialize a session before ingesting documents."
        )
        return

    schema_version = state.schema_version
    if not schema_version:
        st.error(
            "No locked schema found. "
            "Return to Phase 1 to define and lock a domain schema "
            "before ingesting documents."
        )
        return

    st.caption(f"Schema version: `{schema_version}`  |  Run: `{run_id[:16]}…`")
    st.divider()

    # ── Step 1: Upload panel ─────────────────────────────────────────────────
    _render_upload_section(state)
    st.divider()

    # ── Steps 2 & 3: Document list → Chunk manifest viewer ───────────────────
    # _render_document_list fetches fresh from the API on every script run.
    # Its return value is passed directly to _render_chunk_viewer to avoid a
    # second GET /api/documents/{run_id} call within the same render cycle.
    documents = _render_document_list(run_id)
    st.divider()
    _render_chunk_viewer(run_id, documents)

    # ── Proceed ──────────────────────────────────────────────────────────────
    _render_proceed_section(state, has_docs=bool(documents))


if __name__ == "__main__":
    main()
