"""
ui/pages/extraction.py — Phase 3: AI-Assisted Extraction (SPEC-04 S-04.7).

Streamlit page for Phase 3. Allows the user to trigger chunk extraction,
monitor per-run progress, and review the entity summary on completion.

Workflow:
  1. User clicks "Start Extraction" → POST /api/extraction/run.
  2. Page polls GET /api/extraction/status/{run_id} every 2 s while jobs
     are pending, updating the progress bar on each rerun.
  3. When all chunks are resolved (pending == 0), the extraction summary
     and entity preview are revealed.
  4. User clicks "Proceed to Curation" to advance to Phase 4.

Architecture rules:
  - No business logic — pure API calls and display (CLAUDE.md §4.1, SKILL-B).
  - All session state via StateManager.get() — no direct st.session_state
    access anywhere in this file (CLAUDE.md §8).
  - API calls via httpx synchronous client (Streamlit is single-threaded).
  - Panels degrade gracefully when the API is unreachable (SKILL-D R-D12).
  - Auto-poll implemented with time.sleep() + st.rerun() — acceptable in a
    single-user dev environment.

Backend endpoints consumed:
  POST /api/extraction/run              — enqueue all pending chunk jobs
  GET  /api/extraction/status/{run_id} — aggregated job status from Redis
  GET  /api/extraction/results/{run_id} — node/edge counts from Neo4j
"""

import os
import time
from typing import Any

import httpx
import streamlit as st

from api.models.run import Phase
from ui.state import StateManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8000")
_REQUEST_TIMEOUT: float = 10.0
_POLL_INTERVAL_S: int = 2


# ---------------------------------------------------------------------------
# API helpers — all errors return None / error tuple, never raise
# ---------------------------------------------------------------------------


def _fetch(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """GET {_API_BASE_URL}{path} and return parsed JSON or None on any error."""
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as c:
            r = c.get(f"{_API_BASE_URL}{path}", params=params)
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
    except Exception:
        return None


def _post(
    path: str, payload: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """POST {_API_BASE_URL}{path} with JSON body. Returns (data, error)."""
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as c:
            r = c.post(f"{_API_BASE_URL}{path}", json=payload)
            return r.json(), None  # type: ignore[no-any-return]
    except Exception as exc:
        return None, str(exc)


# ---------------------------------------------------------------------------
# Status helpers — pure predicates over the status API response dict
# ---------------------------------------------------------------------------


def _is_running(status: dict[str, Any] | None) -> bool:
    """True when extraction has been started and jobs are still in progress.

    Requires ``queued > 0`` — chunks actively enqueued/running in ARQ.
    Chunks that are merely "pending" (not yet enqueued) do NOT count as
    running, so the Start Extraction button remains visible until clicked.
    """
    if status is None:
        return False
    queued: int = status.get("queued", 0)
    return queued > 0


def _is_done(status: dict[str, Any] | None) -> bool:
    """True when all chunks have been resolved (complete or failed).

    Requires that extraction was started (queued==0, pending==0) and at
    least one chunk exists.
    """
    if status is None:
        return False
    total: int = status.get("total_chunks", 0)
    pending: int = status.get("pending", 0)
    queued: int = status.get("queued", 0)
    return total > 0 and pending == 0 and queued == 0


# ---------------------------------------------------------------------------
# Panel renderers — pure presentation, no state mutations
# ---------------------------------------------------------------------------


def _render_trigger_section(run_id: str, status: dict[str, Any] | None) -> None:
    """Render the extraction trigger button.

    Shows an informational message while extraction is running (button hidden).
    Switches button label to "Re-run Extraction" when all chunks are resolved.
    POST /api/extraction/run is idempotent — already-complete chunks are
    skipped, so re-triggering is always safe.
    """
    st.subheader("Run Extraction")

    if _is_running(status):
        pending = status.get("pending", 0) if status else 0
        st.info(
            f"Extraction in progress — {pending} chunk(s) remaining. "
            "This page refreshes automatically every 2 s."
        )
        return

    if _is_done(status):
        label = "Re-run Extraction"
        help_text = "Re-enqueue chunks that are not yet complete (idempotent)."
    else:
        label = "Start Extraction"
        help_text = (
            "Enqueue one ARQ extraction job per chunk in this run. "
            "Requires the domain schema to be locked (Phase 1)."
        )

    extraction_model = st.text_input(
        "LLM Model (optional)",
        value="openai/gpt-4o-mini",
        help=(
            "OpenRouter model ID for extraction. Leave as default or enter "
            "a different model (e.g. openai/gpt-4o, anthropic/claude-3.5-sonnet)."
        ),
        key="extraction_model",
    )

    if st.button(label, type="primary", help=help_text):
        payload: dict[str, Any] = {"run_id": run_id}
        if extraction_model and extraction_model.strip():
            payload["model"] = extraction_model.strip()
        with st.spinner("Enqueueing extraction jobs…"):
            data, err = _post("/api/extraction/run", payload)

        if err or data is None:
            st.error(f"Failed to start extraction: {err or 'No response from API.'}")
            return

        if data.get("status") == "error":
            for e in data.get("errors", []):
                st.error(f"[{e.get('code')}] {e.get('message')}")
            return

        enqueued: int = data.get("jobs_enqueued", 0)
        if enqueued == 0:
            st.success(
                "All chunks already complete — nothing new to enqueue. "
                "Click below to view results."
            )
        else:
            st.success(
                f"Enqueued {enqueued} extraction job(s). "
                "Monitoring progress below."
            )
        st.rerun()


def _render_progress_section(status: dict[str, Any] | None) -> None:
    """Render the per-run progress bar and chunk count metrics.

    Always shown when extraction has been triggered. Hides gracefully when the
    status endpoint is unreachable.
    """
    st.subheader("Progress")

    if status is None:
        st.warning("Cannot reach extraction status endpoint — progress unavailable.")
        return

    total: int = status.get("total_chunks", 0)
    completed: int = status.get("completed", 0)
    failed: int = status.get("failed", 0)
    pending: int = status.get("pending", 0)

    if total == 0:
        st.info(
            "No chunks found for this run. "
            "Ingest documents in Phase 2 before running extraction."
        )
        return

    resolved = completed + failed
    frac = resolved / total

    col_t, col_c, col_f, col_p = st.columns(4)
    col_t.metric("Total", total)
    col_c.metric("Complete", completed)
    col_f.metric(
        "Failed",
        failed,
        delta=f"-{failed}" if failed else None,
        delta_color="inverse",
    )
    col_p.metric("Pending", pending)

    st.progress(
        frac,
        text=f"{int(frac * 100)}% processed  ({resolved} / {total} chunks)",
    )

    if _is_running(status):
        st.caption(":blue[Extraction running — page auto-refreshes every 2 s.]")


def _render_summary_section(
    status: dict[str, Any] | None,
    results: dict[str, Any] | None,
) -> None:
    """Render extraction summary and entity preview after all jobs resolve.

    Shows completion status (success / partial failure) and entity counts
    read from Neo4j via GET /api/extraction/results/{run_id}.
    """
    st.subheader("Extraction Summary")

    total: int = status.get("total_chunks", 0) if status else 0
    failed: int = status.get("failed", 0) if status else 0

    if failed == 0:
        st.success(f"All {total} chunk(s) processed successfully.")
    else:
        st.warning(
            f"{failed} of {total} chunk(s) failed extraction. "
            "Check the Dashboard for error details."
        )

    st.divider()
    st.markdown("**Entities Written to Neo4j**")

    if results is None:
        st.warning("Cannot reach results endpoint — entity counts unavailable.")
        return

    node_count: int = results.get("node_count", 0)
    edge_count: int = results.get("edge_count", 0)
    schema_version: str = results.get("schema_version", "")

    col_n, col_e = st.columns(2)
    col_n.metric("Nodes", node_count)
    col_e.metric("Relationships", edge_count)

    if schema_version:
        st.caption(f"Schema version: `{schema_version[:16]}…`")

    st.markdown("**Entity Preview**")
    preview = [
        {"Entity type": "Nodes (all labels)", "Count": node_count},
        {"Entity type": "Relationships (all types)", "Count": edge_count},
    ]
    st.dataframe(preview, use_container_width=True, hide_index=True)
    st.caption(
        "Detailed per-type breakdown and entity browsing are available "
        "via Neo4j Browser or the Curation phase."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Extraction page entry point, called by Streamlit on every script rerun."""
    # NOTE: st.set_page_config() is called by app.py before routing here.
    # Calling it again raises StreamlitAPIException in Streamlit 1.40+.

    # All session state access through StateManager (CLAUDE.md §8).
    state = StateManager.get()

    # --- Phase guard: extraction requires Phase 3 ---
    if state.phase.value < Phase.EXTRACTION.value:
        st.title("Phase 3: AI-Assisted Extraction")
        st.warning(
            "Phase 2 (Document Ingestion) must be completed before running "
            "extraction. Ingest documents and click Proceed on the Ingestion page."
        )
        return

    run_id = state.run_id
    if not run_id:
        st.error("No active session. Initialize a session in Phase 0.")
        return

    # --- Sidebar ---
    with st.sidebar:
        st.header("Extraction")
        st.caption("Run ID")
        st.code(run_id[:16] + "…", language=None)
        if state.schema_version:
            st.caption("Schema version")
            st.write(state.schema_version[:16] + "…")
        st.divider()
        st.caption(f"API: `{_API_BASE_URL}`")

    # --- Page header ---
    st.title("Phase 3: AI-Assisted Extraction")
    st.caption(
        "Chunks are sent to the LLM with the locked domain schema. "
        "Extracted entities are validated and written to Neo4j via MERGE."
    )

    # --- Fetch current extraction status (every render) ---
    status = _fetch(f"/api/extraction/status/{run_id}")

    # --- Trigger section ---
    _render_trigger_section(run_id, status)

    st.divider()

    # --- Progress section ---
    _render_progress_section(status)

    # --- Summary + entity preview: shown only when all jobs are resolved ---
    if _is_done(status):
        st.divider()
        results = _fetch(f"/api/extraction/results/{run_id}")
        _render_summary_section(status, results)

        # Offer phase advancement only while still in Phase.EXTRACTION
        if state.phase == Phase.EXTRACTION:
            st.divider()

            def _do_advance() -> None:
                """on_click callback — runs BEFORE the next render cycle."""
                s = StateManager.get()
                if s.phase == Phase.EXTRACTION:
                    s.advance_phase(Phase.CURATION)

            st.button(
                "Proceed to Curation →",
                type="primary",
                on_click=_do_advance,
            )

    # --- Auto-poll while jobs are running (2-second interval) ---
    if _is_running(status):
        time.sleep(_POLL_INTERVAL_S)
        st.rerun()


if __name__ == "__main__":
    main()
