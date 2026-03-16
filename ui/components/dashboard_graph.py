"""
ui/components/dashboard_graph.py — Graph tab for the dashboard.

Displays node/edge count charts using Plotly (upgraded from st.bar_chart).

Architecture rules:
  - No business logic (CLAUDE.md §4.1, SKILL-B R-B3).
  - No st.session_state access outside StateManager (CLAUDE.md §8).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ui.components.api_fetch import fetch
from ui.components.plotly_helpers import bar_chart


def render_graph_tab(run_id: str) -> None:
    """Render the Graph tab content."""
    st.subheader("Graph Statistics")

    node_data = fetch(f"/api/graph/nodes/{run_id}/count")
    edge_data = fetch(f"/api/graph/edges/{run_id}/count")

    if node_data is None and edge_data is None:
        st.warning("Cannot reach API \u2014 graph statistics unavailable.")
        return

    # Total counts
    c1, c2 = st.columns(2)
    c1.metric("Total Nodes", node_data.get("total", 0) if node_data else "\u2014")
    c2.metric("Total Edges", edge_data.get("total", 0) if edge_data else "\u2014")

    st.divider()

    left, right = st.columns(2)

    with left:
        st.markdown("**Nodes by Type**")
        by_type: list[dict[str, Any]] = node_data.get("by_type", []) if node_data else []
        if by_type:
            sorted_types = sorted(by_type, key=lambda x: x.get("count", 0), reverse=True)
            labels = [t.get("type", "?") for t in sorted_types]
            values = [t.get("count", 0) for t in sorted_types]
            fig = bar_chart(labels, values, "Node Counts by Type", horizontal=True)
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No node data.")

    with right:
        st.markdown("**Edges by Type**")
        by_type = edge_data.get("by_type", []) if edge_data else []
        if by_type:
            sorted_types = sorted(by_type, key=lambda x: x.get("count", 0), reverse=True)
            labels = [t.get("type", "?") for t in sorted_types]
            values = [t.get("count", 0) for t in sorted_types]
            fig = bar_chart(labels, values, "Edge Counts by Type", horizontal=True)
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No edge data.")
