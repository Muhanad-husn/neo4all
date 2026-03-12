"""
ui/pages/dashboard.py — Run summary dashboard (SPEC-08 S-08.6).

Read-only page that aggregates data from existing API endpoints into a
single overview. All data fetched from the backend — no business logic.

Sections:
  - Phase indicator (reusable component)
  - Run summary metrics (documents, entities, jobs, proposals)
  - Ingestion pipeline summary (document table with chunk counts)
  - Extraction progress bar (auto-refreshes every 2 s)
  - Graph statistics by type (nodes and edges) with bar charts
  - Recent activity log feed

Architecture rules:
  - No business logic — pure data display (CLAUDE.md §4.1, SKILL-B R-B3).
  - All session state via StateManager.get() (CLAUDE.md §8).
  - No st.session_state access outside StateManager.
  - API calls via httpx (synchronous client — Streamlit is single-threaded).
  - Sections hide gracefully when backend endpoints are unreachable.

Backend endpoints consumed:
  GET /api/documents/{run_id}
  GET /api/graph/nodes/{run_id}/count
  GET /api/graph/edges/{run_id}/count
  GET /api/monitoring/jobs/{run_id}
  GET /api/curation/proposals/{run_id}
  GET /api/monitoring/logs/recent
"""

import os
from datetime import timedelta
from typing import Any

import httpx
import pandas as pd
import streamlit as st

from ui.components.phase_indicator import render_phase_indicator
from ui.state import StateManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8000")
_REQUEST_TIMEOUT: float = 5.0


# ---------------------------------------------------------------------------
# Backend fetch helper
# ---------------------------------------------------------------------------


def _fetch(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """GET {_API_BASE_URL}{path} and return parsed JSON.

    Returns None on any error so callers can degrade gracefully.
    """
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            response = client.get(f"{_API_BASE_URL}{path}", params=params)
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Section renderers — pure presentation, no state mutations
# ---------------------------------------------------------------------------


def _render_run_summary(
    state: StateManager,
    doc_data: dict[str, Any] | None,
    node_data: dict[str, Any] | None,
    job_data: dict[str, Any] | None,
    proposal_data: dict[str, Any] | None,
) -> None:
    """Render 4-column summary metrics for the current run."""
    st.subheader("Run Summary")

    doc_count = len(doc_data.get("documents", [])) if doc_data else "—"
    entity_count = node_data.get("total", "—") if node_data else "—"
    job_total = job_data.get("total", "—") if job_data else "—"
    proposal_count = len(proposal_data.get("proposals", [])) if proposal_data else "—"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Documents", doc_count)
    c2.metric("Entities", entity_count)
    c3.metric("Jobs", job_total)
    c4.metric("Proposals", proposal_count)


def _render_ingestion_summary(
    run_id: str,
    doc_data: dict[str, Any] | None,
) -> None:
    """Render a compact ingestion pipeline summary with per-document chunk counts."""
    st.subheader("Ingestion Pipeline")

    if doc_data is None:
        st.warning("Cannot reach API — ingestion data unavailable.")
        return

    documents: list[dict[str, Any]] = doc_data.get("documents", [])
    total_count: int = doc_data.get("total_count", len(documents))

    if not documents:
        st.info("No documents ingested yet for this run.")
        return

    total_chunks = sum(d.get("chunk_count", 0) for d in documents)

    c1, c2 = st.columns(2)
    c1.metric("Documents Ingested", total_count)
    c2.metric("Total Chunks", total_chunks)

    rows = [
        {
            "Doc ID": (d.get("doc_id") or "")[:16] + "\u2026",
            "Chunks": d.get("chunk_count", 0),
            "Ingested At": d.get("created_at", "\u2014"),
        }
        for d in documents
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_extraction_progress(job_data: dict[str, Any] | None) -> None:
    """Render extraction progress bar with completed/failed/pending metrics."""
    st.subheader("Extraction Progress")

    if job_data is None:
        st.warning("Cannot reach API — extraction progress unavailable.")
        return

    total: int = job_data.get("total", 0)
    completed: int = job_data.get("completed", 0)
    failed: int = job_data.get("failed", 0)
    pending: int = job_data.get("pending", 0)

    if total == 0:
        st.info("No extraction jobs found for this run.")
        return

    resolved = completed + failed
    frac = resolved / total

    st.progress(frac, text=f"{int(frac * 100)}% resolved  ({resolved} / {total} chunks)")

    c1, c2, c3 = st.columns(3)
    c1.metric("Completed", completed)
    c2.metric("Failed", failed)
    c3.metric("Pending", pending)


def _render_graph_stats(
    node_data: dict[str, Any] | None,
    edge_data: dict[str, Any] | None,
) -> None:
    """Render node/edge type breakdowns as bar charts."""
    st.subheader("Graph Statistics")

    if node_data is None and edge_data is None:
        st.warning("Cannot reach API — graph statistics unavailable.")
        return

    left, right = st.columns(2)

    with left:
        st.markdown("**Node Counts by Type**")
        by_type = node_data.get("by_type", []) if node_data else []
        if by_type:
            df = pd.DataFrame(by_type).rename(columns={"type": "Type", "count": "Count"})
            st.bar_chart(df, x="Type", y="Count")
        else:
            st.info("No node data.")

    with right:
        st.markdown("**Edge Counts by Type**")
        by_type = edge_data.get("by_type", []) if edge_data else []
        if by_type:
            df = pd.DataFrame(by_type).rename(columns={"type": "Type", "count": "Count"})
            st.bar_chart(df, x="Type", y="Count")
        else:
            st.info("No edge data.")


def _render_recent_activity(log_data: dict[str, Any] | None) -> None:
    """Render last 10 log entries as a compact table."""
    st.subheader("Recent Activity")

    if log_data is None:
        st.warning("Cannot reach API — recent logs unavailable.")
        return

    entries: list[dict[str, Any]] = log_data.get("entries", [])
    if not entries:
        st.info("No recent log entries.")
        return

    rows = [
        {
            "Timestamp": e.get("timestamp", "—"),
            "Level": e.get("level", "—"),
            "Event": e.get("event", "—"),
        }
        for e in entries[:10]
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Page entry point
# ---------------------------------------------------------------------------


@st.fragment
def _fragment_run_summary(run_id: str, state: StateManager) -> None:
    """Fragment: fetches and renders the run summary metrics independently."""
    doc_data = _fetch(f"/api/documents/{run_id}")
    node_data = _fetch(f"/api/graph/nodes/{run_id}/count")
    job_data = _fetch(f"/api/monitoring/jobs/{run_id}")
    proposal_data = _fetch(f"/api/curation/proposals/{run_id}")
    _render_run_summary(state, doc_data, node_data, job_data, proposal_data)


@st.fragment
def _fragment_ingestion_summary(run_id: str) -> None:
    """Fragment: fetches and renders ingestion pipeline summary independently."""
    doc_data = _fetch(f"/api/documents/{run_id}")
    _render_ingestion_summary(run_id, doc_data)


@st.fragment(run_every=timedelta(seconds=2))
def _fragment_extraction_progress(run_id: str) -> None:
    """Fragment: fetches and renders extraction progress independently.

    Auto-refreshes every 2 s so extraction progress streams live, matching the
    extraction page's polling behaviour.
    """
    job_data = _fetch(f"/api/monitoring/jobs/{run_id}")
    _render_extraction_progress(job_data)


@st.fragment
def _fragment_graph_stats(run_id: str) -> None:
    """Fragment: fetches and renders graph statistics independently."""
    node_data = _fetch(f"/api/graph/nodes/{run_id}/count")
    edge_data = _fetch(f"/api/graph/edges/{run_id}/count")
    _render_graph_stats(node_data, edge_data)


@st.fragment
def _fragment_recent_activity() -> None:
    """Fragment: fetches and renders recent activity independently."""
    log_data = _fetch("/api/monitoring/logs/recent", params={"limit": 10})
    _render_recent_activity(log_data)


def main() -> None:
    """Dashboard page entry point."""
    st.title("Dashboard")

    state = StateManager.get()
    render_phase_indicator(state.phase.value)

    st.divider()

    if state.run_id is None:
        st.info("Start a session to see dashboard data.")
        return

    run_id = state.run_id
    st.caption(f"Run: `{run_id}`")

    # Each section is an independent fragment that fetches its own data.
    # If one API call is slow, other sections still render promptly.
    _fragment_run_summary(run_id, state)
    st.divider()
    _fragment_ingestion_summary(run_id)
    st.divider()
    _fragment_extraction_progress(run_id)
    st.divider()
    _fragment_graph_stats(run_id)
    st.divider()
    _fragment_recent_activity()


if __name__ == "__main__":
    main()
