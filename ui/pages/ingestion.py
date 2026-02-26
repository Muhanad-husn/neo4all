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

from typing import Any

import streamlit as st

from api.models.run import Phase
from ui.pages.ingestion_helpers import (
    _ACCEPTED_EXTENSIONS,
    _docs_to_df,
    _fmt_file_size,
    _get,
    _handle_ingest,
    _render_single_doc_chunks,
    _show_api_errors,
)
from ui.state import StateManager


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

    # Guard: only show the proceed button when the phase is still INGESTION.
    # Prevents errors if the user navigates here via Streamlit multipage
    # sidebar after already advancing past Phase 2.
    if state.phase != Phase.INGESTION:
        st.info(
            "You have already proceeded past Phase 2.",
            icon="✅",
        )
        return

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
