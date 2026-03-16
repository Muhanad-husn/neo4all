"""
ui/pages/graph_explorer.py — Graph Explorer: paginated browse of Neo4j graph (SPEC-06 S-06.9).

Read-only, paginated views over all nodes and edges in the current run's
Neo4j graph.  Data is served from GraphReader (cache-first, 5-min TTL).
No mutations — display only.

Workflow:
  Nodes tab:
    1. GET /api/graph/nodes/{run_id}/count  — totals by node_type.
    2. GET /api/graph/nodes/{run_id}?page=N&page_size=50&node_type=...
       — paginated list, default 50 per page (max 5000 with 'Show all').

  Edges tab:
    1. GET /api/graph/edges/{run_id}/count  — totals by rel_type.
    2. GET /api/graph/edges/{run_id}?page=N&page_size=50&edge_type=...
       — paginated list, default 50 per page (max 5000 with 'Show all').

Architecture rules:
  - No business logic — pure API calls and display (CLAUDE.md §4.1, SKILL-B).
  - All session state via StateManager.get() (CLAUDE.md §8).
  - Page selection via st.number_input — Streamlit persists widget state
    internally via the widget key.  The widget return value is read directly
    (no direct st.session_state access).
  - API calls via httpx synchronous client (Streamlit is single-threaded).
  - Panels degrade gracefully when the API is unreachable (SKILL-D R-D12).
  - Read-only page — no mutations, no business logic (SKILL-D R-D12).
"""

import os
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
_PAGE_SIZE: int = 50

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _fetch(
    path: str, params: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """GET {_API_BASE_URL}{path} and return parsed JSON or None on any error."""
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as c:
            r = c.get(f"{_API_BASE_URL}{path}", params=params)
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _parse_primary_value(dedupe_key: str) -> str:
    """Extract the primary value from a pipe-delimited dedupe key.

    Node dedupe keys follow the format ``NodeType|primary_value|schema_version``.
    Returns the middle segment, or the truncated key if no pipes are present.
    """
    parts = dedupe_key.split("|")
    if len(parts) >= 3:
        return parts[1]
    return dedupe_key[:40]


def _show_api_error(data: dict[str, Any]) -> None:
    """Render structured API error details."""
    for e in data.get("errors", []):
        st.error(f"[{e.get('code', 'error')}] {e.get('message', 'Unknown error.')}")


def _render_type_breakdown(by_type: list[dict[str, Any]], label: str) -> None:
    """Render a compact per-type count table."""
    if not by_type:
        return
    rows = [{"Type": entry["type"], "Count": entry["count"]} for entry in by_type]
    st.markdown(f"**{label}**")
    st.dataframe(rows, width="stretch", hide_index=True)


def _page_selector(*, total: int, page_size: int, key: str) -> int:
    """Render a page number input and return the selected page (1-indexed).

    Uses st.number_input whose value is persisted automatically by Streamlit
    via the widget key — no direct st.session_state access required.

    Args:
        total:     Total items (after type filtering, before paging).
        page_size: Items per page (backend max 50).
        key:       Unique widget key (e.g. "nodes_page" / "edges_page").

    Returns:
        Current 1-indexed page number.
    """
    total_pages = max(1, (total + page_size - 1) // page_size)
    col_input, col_info = st.columns([1, 4])
    with col_input:
        page: int = st.number_input(
            "Page",
            min_value=1,
            max_value=total_pages,
            value=1,
            step=1,
            key=key,
        )
    with col_info:
        st.caption(f"of {total_pages} page(s)  |  {total} item(s) total")
    return int(page)


# ---------------------------------------------------------------------------
# Node browser
# ---------------------------------------------------------------------------


def _render_node_browser(run_id: str) -> None:
    """Render the paginated node browser with optional type filter."""

    # --- Step 1: Count + type breakdown ---
    count_data = _fetch(f"/api/graph/nodes/{run_id}/count")

    if count_data is None:
        st.warning("Cannot reach the node count endpoint — check API availability.")
        return

    if count_data.get("status") == "error":
        _show_api_error(count_data)
        return

    total: int = count_data.get("total", 0)
    by_type: list[dict[str, Any]] = count_data.get("by_type", [])

    col_total, col_filter = st.columns([1, 3])
    col_total.metric("Total Nodes", total)

    # Build filter options from the count response.
    type_options: list[str] = ["(all types)"] + [e["type"] for e in by_type]
    with col_filter:
        node_type_filter: str = st.selectbox(
            "Filter by node type",
            type_options,
            key="node_type_filter",
        )

    _render_type_breakdown(by_type, "Nodes by type")

    if total == 0:
        st.info(
            "No nodes found for this run.  "
            "Complete Phase 3 (Extraction) to populate the graph."
        )
        return

    st.divider()

    # --- Step 2: Show-all toggle + page selector ---
    show_all: bool = st.toggle("Show all rows", key="nodes_show_all")

    if show_all:
        # Fetch all items in a single request.
        params: dict[str, Any] = {"page": 1, "page_size": max(total, 1)}
        if node_type_filter != "(all types)":
            params["node_type"] = node_type_filter
    else:
        current_page: int = _page_selector(
            total=total if node_type_filter == "(all types)" else total,
            page_size=_PAGE_SIZE,
            key="nodes_page",
        )
        params = {"page": current_page, "page_size": _PAGE_SIZE}
        if node_type_filter != "(all types)":
            params["node_type"] = node_type_filter

    # --- Step 3: Fetch data ---
    page_data = _fetch(f"/api/graph/nodes/{run_id}", params=params)

    if page_data is None:
        st.warning("Cannot reach the node list endpoint.")
        return

    if page_data.get("status") == "error":
        _show_api_error(page_data)
        return

    items: list[dict[str, Any]] = page_data.get("items", [])
    page_total: int = page_data.get("total", 0)
    has_more: bool = page_data.get("has_more", False)

    if not items:
        st.info("No nodes on this page.")
        return

    if show_all:
        st.caption(f"Showing all {page_total} node(s)")
    else:
        if node_type_filter != "(all types)":
            st.caption(f"Filtered total: {page_total} node(s) of type '{node_type_filter}'")
        if has_more:
            st.caption(f"Showing {_PAGE_SIZE} of {page_total} — use the page selector above.")

    # --- Step 4: Display table ---
    rows = [
        {
            "Node type": item.get("node_type", ""),
            "Dedupe key": item.get("dedupe_key", "")[:50],
            "Primary value": item.get("properties", {}).get("_primary_value", ""),
            "Schema version": item.get("schema_version", "")[:12] + "…",
            "Properties (preview)": str(item.get("properties", {}))[:80],
        }
        for item in items
    ]
    st.dataframe(rows, width="stretch", hide_index=True)

    # --- Step 5: Full property detail in expandable section ---
    with st.expander(f"Full property view — {len(items)} node(s)"):
        for item in items:
            dk = item.get("dedupe_key", "")
            nt = item.get("node_type", "")
            with st.expander(f"{nt}  `{dk[:40]}…`", expanded=False):
                st.caption(f"Dedupe key: `{dk}`")
                st.json(item.get("properties", {}))


# ---------------------------------------------------------------------------
# Edge browser
# ---------------------------------------------------------------------------


def _render_edge_browser(run_id: str) -> None:
    """Render the paginated edge browser with optional type filter."""

    # --- Step 1: Count + type breakdown ---
    count_data = _fetch(f"/api/graph/edges/{run_id}/count")

    if count_data is None:
        st.warning("Cannot reach the edge count endpoint — check API availability.")
        return

    if count_data.get("status") == "error":
        _show_api_error(count_data)
        return

    total: int = count_data.get("total", 0)
    by_type: list[dict[str, Any]] = count_data.get("by_type", [])

    col_total, col_filter = st.columns([1, 3])
    col_total.metric("Total Edges", total)

    type_options: list[str] = ["(all types)"] + [e["type"] for e in by_type]
    with col_filter:
        edge_type_filter: str = st.selectbox(
            "Filter by edge type",
            type_options,
            key="edge_type_filter",
        )

    _render_type_breakdown(by_type, "Edges by type")

    if total == 0:
        st.info(
            "No edges found for this run.  "
            "Complete Phase 3 (Extraction) to populate the graph."
        )
        return

    st.divider()

    # --- Step 2: Show-all toggle + page selector ---
    show_all: bool = st.toggle("Show all rows", key="edges_show_all")

    if show_all:
        params: dict[str, Any] = {"page": 1, "page_size": max(total, 1)}
        if edge_type_filter != "(all types)":
            params["edge_type"] = edge_type_filter
    else:
        current_page: int = _page_selector(
            total=total,
            page_size=_PAGE_SIZE,
            key="edges_page",
        )
        params = {"page": current_page, "page_size": _PAGE_SIZE}
        if edge_type_filter != "(all types)":
            params["edge_type"] = edge_type_filter

    # --- Step 3: Fetch data ---
    page_data = _fetch(f"/api/graph/edges/{run_id}", params=params)

    if page_data is None:
        st.warning("Cannot reach the edge list endpoint.")
        return

    if page_data.get("status") == "error":
        _show_api_error(page_data)
        return

    items: list[dict[str, Any]] = page_data.get("items", [])
    page_total: int = page_data.get("total", 0)
    has_more: bool = page_data.get("has_more", False)

    if not items:
        st.info("No edges on this page.")
        return

    if show_all:
        st.caption(f"Showing all {page_total} edge(s)")
    else:
        if edge_type_filter != "(all types)":
            st.caption(f"Filtered total: {page_total} edge(s) of type '{edge_type_filter}'")
        if has_more:
            st.caption(f"Showing {_PAGE_SIZE} of {page_total} — use the page selector above.")

    # --- Step 4: Display table ---
    rows = [
        {
            "Rel type": item.get("rel_type", ""),
            "Start node": _parse_primary_value(item.get("start_dedupe_key", "")),
            "End node": _parse_primary_value(item.get("end_dedupe_key", "")),
            "Schema version": item.get("schema_version", "")[:12] + "…",
            "Properties (preview)": str(item.get("properties", {}))[:60],
        }
        for item in items
    ]
    st.dataframe(rows, width="stretch", hide_index=True)

    # --- Step 5: Full property detail in expandable section ---
    with st.expander(f"Full property view — {len(items)} edge(s)"):
        for item in items:
            dk = item.get("dedupe_key", "")
            rt = item.get("rel_type", "")
            start = item.get("start_dedupe_key", "")[:30]
            end = item.get("end_dedupe_key", "")[:30]
            with st.expander(f"{rt}  `{start}…` → `{end}…`", expanded=False):
                st.caption(
                    f"Start: `{item.get('start_dedupe_key', '')}`\n"
                    f"End:   `{item.get('end_dedupe_key', '')}`"
                )
                st.caption(f"Dedupe key: `{dk}`")
                st.json(item.get("properties", {}))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Graph explorer page entry point, called by Streamlit on every script rerun."""
    # NOTE: st.set_page_config() is called by app.py before routing here.
    # Calling it again raises StreamlitAPIException in Streamlit 1.40+.

    state = StateManager.get()

    if state.phase.value < Phase.CURATION.value:
        st.title("Graph Explorer")
        st.warning(
            "Graph Explorer is available from Phase 4 (Curation) onwards. "
            "Complete extraction and proceed to the Curation page first."
        )
        return

    run_id = state.run_id
    if not run_id:
        st.error("No active session.  Initialize a session in Phase 0.")
        return

    with st.sidebar:
        st.header("Graph Explorer")
        st.caption("Run ID")
        st.code(run_id[:16] + "…", language=None)
        if state.schema_version:
            st.caption("Schema version")
            st.write(state.schema_version[:16] + "…")
        st.divider()
        st.caption(f"API: `{_API_BASE_URL}`")
        st.caption("Read-only — no mutations.")

    st.title("Graph Explorer")
    st.caption(
        "Read-only view of all nodes and edges in the Neo4j graph "
        "for this run.  GraphReader cache (5-minute TTL).  "
        "Toggle 'Show all rows' to view the complete dataset."
    )

    tab_nodes, tab_edges = st.tabs(["Nodes", "Edges"])

    with tab_nodes:
        _render_node_browser(run_id)

    with tab_edges:
        _render_edge_browser(run_id)


if __name__ == "__main__":
    main()
