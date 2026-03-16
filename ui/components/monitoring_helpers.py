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

from collections.abc import Callable
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


def render_failed_chunks_expander(
    jobs: list[dict[str, Any]],
    run_id: str | None = None,
    fetch_text_fn: Callable[[str, str], str | None] | None = None,
) -> None:
    """Show an expander listing failed extraction chunks with error details.

    Filters to status=='failed' jobs and displays chunk_id, doc_id prefix,
    and the error message for each.

    Args:
        jobs: Per-chunk job details list.
        run_id: Current run ID (needed for chunk text fetch).
        fetch_text_fn: Optional callback ``(run_id, chunk_id) -> text | None``
            to retrieve the source text of a failed chunk for review.
    """
    failed = [j for j in jobs if j.get("status") == "failed"]
    if not failed:
        return

    with st.expander(f"Failed chunks ({len(failed)})", expanded=False):
        for idx, j in enumerate(failed):
            chunk_id = j.get("chunk_id") or ""
            chunk_id_short = chunk_id[:16]
            doc_id = (j.get("doc_id") or "")[:16]
            error = j.get("error") or "Unknown error"
            st.markdown(f"- `{chunk_id_short}…` (doc `{doc_id}…`) — {error}")

            if fetch_text_fn is not None and run_id and chunk_id:
                btn_key = f"view_chunk_text_{idx}_{chunk_id_short}"
                if st.button("View chunk text", key=btn_key):
                    text = fetch_text_fn(run_id, chunk_id)
                    if text:
                        st.code(text, language=None)
                    else:
                        st.caption("Could not retrieve chunk text.")


def render_entity_yield_metrics(jobs: list[dict[str, Any]]) -> None:
    """Sum node and edge totals (created + matched) from completed jobs.

    Only counts jobs with status=='complete'.  Both ``merge_created`` and
    ``merge_matched`` actions represent entities written for this run, so
    both are included to stay consistent with the Neo4j entity counts shown
    in the Extraction Summary section.
    """
    completed = [j for j in jobs if j.get("status") == "complete"]
    if not completed:
        return

    total_nodes = sum(
        j.get("nodes_created", 0) + j.get("nodes_matched", 0)
        for j in completed
    )
    total_edges = sum(
        j.get("edges_created", 0) + j.get("edges_matched", 0)
        for j in completed
    )

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
