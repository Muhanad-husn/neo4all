"""
ui/components/dashboard_schema.py — Schema tab for the dashboard.

Displays the locked domain schema (node & edge types) in a read-only
two-column layout with a donut chart showing node type distribution.

Architecture rules:
  - No business logic (CLAUDE.md §4.1, SKILL-B R-B3).
  - No st.session_state access outside StateManager (CLAUDE.md §8).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from ui.components.api_fetch import fetch
from ui.components.plotly_helpers import donut_chart


def render_schema_tab(run_id: str) -> None:
    """Render the Schema tab content."""
    st.subheader("Domain Schema")

    data = fetch(f"/api/schema/{run_id}")

    if data is None or data.get("status") == "error":
        st.info("Schema not available yet. Lock a schema in Phase 1 to see it here.")
        return

    version = data.get("version_hash", "")
    nodes: list[dict[str, Any]] = data.get("nodes", [])
    edges: list[dict[str, Any]] = data.get("edges", [])

    # Header metrics
    c1, c2, c3 = st.columns(3)
    c1.metric("Schema Version", (version[:16] + "\u2026") if version else "\u2014")
    c2.metric("Node Types", len(nodes))
    c3.metric("Edge Types", len(edges))

    st.divider()

    # Two-column tables
    left, right = st.columns(2)

    with left:
        st.markdown("**Node Types**")
        if nodes:
            rows = [
                {
                    "Label": n.get("label", ""),
                    "Primary Property": n.get("primary_property", ""),
                    "Properties": len(n.get("properties", [])),
                }
                for n in nodes
            ]
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.caption("No node types defined.")

    with right:
        st.markdown("**Edge Types**")
        if edges:
            rows = [
                {
                    "Type": e.get("type", ""),
                    "Start": e.get("start_node_type", ""),
                    "End": e.get("end_node_type", ""),
                }
                for e in edges
            ]
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.caption("No edge types defined.")

    # Donut chart: node types by label
    if nodes:
        labels = [n.get("label", "?") for n in nodes]
        values = [len(n.get("properties", [])) for n in nodes]
        if any(v > 0 for v in values):
            fig = donut_chart(labels, values, "Node Types by Property Count")
            st.plotly_chart(fig, use_container_width=True)
