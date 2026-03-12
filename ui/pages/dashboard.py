"""
ui/pages/dashboard.py — Run summary dashboard (SPEC-08 S-08.6).

Read-only page that aggregates data from existing API endpoints into a
single overview. All data fetched from the backend — no business logic.

Sections:
  - Phase indicator (reusable component)
  - Run summary metrics (documents, entities, jobs, proposals)
  - Extraction progress bar
  - Graph statistics by type (nodes and edges) with bar charts
  - Proposal status breakdown with resolution progress bar
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


def _render_proposal_summary(proposal_data: dict[str, Any] | None) -> None:
    """Render proposal status breakdown with resolution progress bar."""
    st.subheader("Proposal Summary")

    if proposal_data is None:
        st.warning("Cannot reach API — proposal data unavailable.")
        return

    proposals: list[dict[str, Any]] = proposal_data.get("proposals", [])
    total = len(proposals)
    approved = sum(1 for p in proposals if p.get("status") == "approved")
    rejected = sum(
        1 for p in proposals if p.get("status") in ("rejected", "failed")
    )

    c1, c2, c3 = st.columns(3)
    c1.metric("Total", total)
    c2.metric("Approved", approved)
    c3.metric("Rejected / Failed", rejected)

    if total > 0:
        resolved = approved + rejected
        frac = resolved / total
        pending = total - resolved
        st.progress(frac, text=f"{int(frac * 100)}% resolved  ({resolved} / {total})")
        if pending > 0:
            st.caption(f"{pending} proposal(s) still pending.")


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


def main() -> None:
    """Dashboard page entry point."""
    st.title("Dashboard")

    state = StateManager.get()
    render_phase_indicator(state.phase.value)

    st.divider()

    if state.run_id is None:
        st.info("Start a session to see dashboard data.")
        return

    st.caption(f"Run: `{state.run_id}`")

    # Fetch all data in sequence (Streamlit is single-threaded).
    doc_data = _fetch(f"/api/documents/{state.run_id}")
    node_data = _fetch(f"/api/graph/nodes/{state.run_id}/count")
    edge_data = _fetch(f"/api/graph/edges/{state.run_id}/count")
    job_data = _fetch(f"/api/monitoring/jobs/{state.run_id}")
    proposal_data = _fetch(f"/api/curation/proposals/{state.run_id}")
    log_data = _fetch("/api/monitoring/logs/recent", params={"limit": 10})

    _render_run_summary(state, doc_data, node_data, job_data, proposal_data)
    st.divider()
    _render_extraction_progress(job_data)
    st.divider()
    _render_graph_stats(node_data, edge_data)
    st.divider()
    _render_proposal_summary(proposal_data)
    st.divider()
    _render_recent_activity(log_data)


if __name__ == "__main__":
    main()
