"""
ui/components/monitoring_helpers.py — Shared monitoring display helpers.

Reusable presentation functions used by dashboard.py, extraction.py, and
other pipeline pages. Avoids duplication of progress/error rendering logic.

Architecture rules:
  - Pure display helpers — no business logic (CLAUDE.md §4.1, SKILL-B R-B3).
  - No st.session_state access (CLAUDE.md §8).
  - No imports from api/graph/, api/agents/, api/vector/, api/diff/, api/audit/.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import streamlit as st


def format_duration(started_at: str | None, completed_at: str | None) -> str:
    """Convert two ISO timestamps to a human-readable 'Xm Ys' duration string.

    Returns '' if either timestamp is missing or unparseable.
    """
    if not started_at or not completed_at:
        return ""
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(completed_at)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        total_seconds = int((end - start).total_seconds())
        if total_seconds < 0:
            return ""
        minutes, seconds = divmod(total_seconds, 60)
        if minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"
    except (ValueError, TypeError):
        return ""


def render_failed_chunks_expander(jobs: list[dict[str, Any]]) -> None:
    """Show an expander listing failed extraction chunks with error details.

    Filters to status=='failed' jobs and displays chunk_id, doc_id prefix,
    and the error message for each.
    """
    failed = [j for j in jobs if j.get("status") == "failed"]
    if not failed:
        return

    with st.expander(f"Failed chunks ({len(failed)})", expanded=False):
        for j in failed:
            chunk_id = (j.get("chunk_id") or "")[:16]
            doc_id = (j.get("doc_id") or "")[:16]
            error = j.get("error") or "Unknown error"
            st.markdown(f"- `{chunk_id}…` (doc `{doc_id}…`) — {error}")


def render_entity_yield_metrics(jobs: list[dict[str, Any]]) -> None:
    """Sum nodes_created and edges_created from completed jobs and render as metrics.

    Only counts jobs with status=='completed'.
    """
    completed = [j for j in jobs if j.get("status") == "completed"]
    if not completed:
        return

    total_nodes = sum(j.get("nodes_created", 0) for j in completed)
    total_edges = sum(j.get("edges_created", 0) for j in completed)

    col_n, col_e = st.columns(2)
    col_n.metric("Nodes extracted so far", total_nodes)
    col_e.metric("Edges extracted so far", total_edges)


def render_worker_cache_row(
    worker_data: dict[str, Any] | None,
    cache_data: dict[str, Any] | None,
) -> None:
    """Render worker queue stats and cache stats in a two-column layout.

    Degrades gracefully when either data source is None.
    """
    col_w, col_c = st.columns(2)

    with col_w:
        st.markdown("**Worker**")
        if worker_data is None:
            st.caption("Worker data unavailable.")
        else:
            w1, w2, w3 = st.columns(3)
            w1.metric("Queue depth", worker_data.get("queue_depth", "—"))
            w2.metric("Active", worker_data.get("active_jobs", "—"))
            w3.metric("Total processed", worker_data.get("total_processed", "—"))

    with col_c:
        st.markdown("**Cache**")
        if cache_data is None:
            st.caption("Cache data unavailable.")
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("Keys", cache_data.get("total_keys", "—"))
            c2.metric("Memory", cache_data.get("used_memory_human", "—"))
            hit_ratio = cache_data.get("hit_ratio")
            c3.metric(
                "Hit ratio",
                f"{hit_ratio:.0%}" if isinstance(hit_ratio, (int, float)) else "—",
            )
