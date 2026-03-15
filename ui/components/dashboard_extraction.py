"""
ui/components/dashboard_extraction.py — Extraction tab for the dashboard.

Shows extraction progress, per-chunk entity breakdown, model info, and
failed chunk details.  The progress section auto-refreshes every 2 s.

Architecture rules:
  - No business logic (CLAUDE.md §4.1, SKILL-B R-B3).
  - No st.session_state access outside StateManager (CLAUDE.md §8).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import streamlit as st

from ui.components.api_fetch import fetch
from ui.components.monitoring_helpers import (
    render_entity_yield_metrics,
    render_failed_chunks_expander,
)
from ui.components.plotly_helpers import gauge_chart, stacked_bar
from ui.state import StateManager


def render_extraction_tab(run_id: str) -> None:
    """Render the Extraction tab content."""
    st.subheader("Extraction Progress")

    # Model info from StateManager
    state = StateManager.get()
    model = state.extraction_model
    if model:
        st.caption(f"Model: `{model}`")

    _extraction_auto_refresh(run_id)


@st.fragment(run_every=timedelta(seconds=2))
def _extraction_auto_refresh(run_id: str) -> None:
    """Auto-refreshing fragment for extraction progress."""
    job_data = fetch(f"/api/monitoring/jobs/{run_id}")

    if job_data is None:
        st.info("No extraction data available yet.")
        return

    total: int = job_data.get("total", 0)
    completed: int = job_data.get("completed", 0)
    failed: int = job_data.get("failed", 0)
    pending: int = job_data.get("pending", 0)

    if total == 0:
        st.info("No extraction jobs found for this run.")
        return

    resolved = completed + failed
    frac = resolved / total if total > 0 else 0.0

    # Gauge chart
    fig = gauge_chart(frac, "Extraction Completion")
    st.plotly_chart(fig, use_container_width=True)

    # Status metrics
    c1, c2, c3 = st.columns(3)
    c1.metric("Completed", completed)
    c2.metric("Failed", failed)
    c3.metric("Pending", pending)

    # Per-chunk entity breakdown
    jobs_list: list[dict[str, Any]] = job_data.get("jobs", [])
    if jobs_list:
        completed_jobs = [j for j in jobs_list if j.get("status") == "complete"]
        if completed_jobs:
            chunk_labels = [
                (j.get("chunk_id") or "")[:12] + "\u2026" for j in completed_jobs
            ]
            nodes_vals = [j.get("nodes_created", 0) for j in completed_jobs]
            edges_vals = [j.get("edges_created", 0) for j in completed_jobs]

            fig = stacked_bar(
                chunk_labels,
                {"Nodes": nodes_vals, "Edges": edges_vals},
                "Entities per Chunk",
                horizontal=True,
            )
            st.plotly_chart(fig, use_container_width=True)

        # Entity yield metrics
        render_entity_yield_metrics(jobs_list)

        # Failed chunks
        if failed > 0:
            render_failed_chunks_expander(jobs_list)
