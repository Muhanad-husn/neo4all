"""
ui/components/dashboard_overview.py — Overview tab for the dashboard.

Migrates the existing run-summary, worker/cache, and recent activity
sections from the monolithic dashboard into a single tab renderer.

Architecture rules:
  - No business logic (CLAUDE.md §4.1, SKILL-B R-B3).
  - No st.session_state access outside StateManager (CLAUDE.md §8).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ui.components.activity_feed import _relative_time, _severity_dot
from ui.components.api_fetch import fetch
from ui.components.monitoring_helpers import render_worker_cache_row
from ui.components.plotly_helpers import gauge_chart
from ui.state import StateManager


def render_overview_tab(run_id: str, state: StateManager) -> None:
    """Render the Overview tab content."""
    _render_run_summary(run_id, state)
    st.divider()
    _render_worker_cache(run_id)
    st.divider()
    _render_recent_activity(run_id)


def _render_run_summary(run_id: str, state: StateManager) -> None:
    """4-column summary metrics."""
    st.subheader("Run Summary")

    doc_data = fetch(f"/api/documents/{run_id}")
    node_data = fetch(f"/api/graph/nodes/{run_id}/count")
    job_data = fetch(f"/api/monitoring/jobs/{run_id}")
    proposal_data = fetch(f"/api/curation/proposals/{run_id}")

    doc_count = len(doc_data.get("documents", [])) if doc_data else "\u2014"
    entity_count = node_data.get("total", "\u2014") if node_data else "\u2014"
    job_total = job_data.get("total", "\u2014") if job_data else "\u2014"
    proposal_count = (
        len(proposal_data.get("proposals", [])) if proposal_data else "\u2014"
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Documents", doc_count)
    c2.metric("Entities", entity_count)
    c3.metric("Jobs", job_total)
    c4.metric("Proposals", proposal_count)


def _render_worker_cache(run_id: str) -> None:
    """Worker & cache status with cache hit-ratio gauge."""
    st.subheader("Worker & Cache")
    worker_data = fetch("/api/monitoring/workers")
    cache_data = fetch("/api/monitoring/cache")
    render_worker_cache_row(worker_data, cache_data)

    if cache_data is not None:
        hit_ratio = cache_data.get("hit_ratio")
        if isinstance(hit_ratio, (int, float)):
            fig = gauge_chart(hit_ratio, "Cache Hit Ratio")
            st.plotly_chart(fig, width="stretch")


def _render_recent_activity(run_id: str) -> None:
    """Run-scoped activity log feed with level filter."""
    st.subheader("Recent Activity")

    level_filter = st.selectbox(
        "Filter by level",
        ["All", "INFO", "WARNING", "ERROR"],
        key="dashboard_overview_activity_level",
    )

    log_data = fetch(
        "/api/monitoring/logs/activity",
        params={"run_id": run_id, "limit": 20},
    )

    if log_data is None:
        st.warning("Cannot reach API \u2014 recent logs unavailable.")
        return

    entries: list[dict[str, Any]] = log_data.get("entries", [])
    if not entries:
        st.info("No recent activity for this run.")
        return

    if level_filter != "All":
        entries = [
            e for e in entries if e.get("level", "").upper() == level_filter.upper()
        ]

    if not entries:
        st.info(f"No {level_filter} entries found.")
        return

    for entry in entries[:20]:
        level = str(entry.get("level", "info"))
        event = str(entry.get("event", "unknown")).replace("_", " ")
        timestamp = entry.get("timestamp", "")
        rel_time = _relative_time(timestamp)
        dot = _severity_dot(level)
        line = f"{dot} {event}"
        if rel_time:
            line += f"  :gray[{rel_time}]"
        st.markdown(line)
