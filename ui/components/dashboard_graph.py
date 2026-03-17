"""
ui/components/dashboard_graph.py — Graph tab for the dashboard.

Displays node/edge count charts and a node centrality (degree) chart.

Architecture rules:
  - No business logic (CLAUDE.md §4.1, SKILL-B R-B3).
  - No st.session_state access outside StateManager (CLAUDE.md §8).
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
import streamlit as st

from ui.components.api_fetch import fetch
from ui.components.plotly_helpers import COLORS, _base_layout, bar_chart


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
            st.plotly_chart(fig, use_container_width=True)
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
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No edge data.")

    # --- Node Centrality (Degree) Chart ---
    st.divider()
    _render_centrality_chart(run_id)


def _render_centrality_chart(run_id: str) -> None:
    """Render an interactive node centrality chart based on degree data."""
    st.markdown("**Node Centrality (Degree)**")
    st.caption(
        "Bubble size reflects total degree (in + out connections). "
        "High-centrality nodes act as key hubs in the knowledge graph."
    )

    degree_data = fetch(f"/api/graph/nodes/{run_id}/degrees")
    if degree_data is None:
        st.info("Degree data unavailable \u2014 check API availability.")
        return

    if degree_data.get("status") == "error":
        for e in degree_data.get("errors", []):
            st.error(f"[{e.get('code', 'error')}] {e.get('message', '')}")
        return

    items: list[dict[str, Any]] = degree_data.get("items", [])
    if not items:
        st.info("No degree data available yet.")
        return

    # Collect unique node types for colour mapping.
    node_types = sorted({it["node_type"] for it in items if it.get("node_type")})
    type_color: dict[str, str] = {
        nt: COLORS[i % len(COLORS)] for i, nt in enumerate(node_types)
    }

    # Build one scatter trace per node type for a clean legend.
    fig = go.Figure()
    max_degree = max(it["total_degree"] for it in items) if items else 1

    for nt in node_types:
        nt_items = [it for it in items if it["node_type"] == nt]
        # Scale bubble sizes: min 8, max 50.
        sizes = [
            max(8, int(50 * it["total_degree"] / max_degree)) for it in nt_items
        ]
        hover_texts = [
            (
                f"<b>{it.get('primary_value') or it['dedupe_key'][:30]}</b><br>"
                f"Type: {it['node_type']}<br>"
                f"In: {it['in_degree']}  |  Out: {it['out_degree']}  |  "
                f"Total: {it['total_degree']}"
            )
            for it in nt_items
        ]
        fig.add_trace(
            go.Scatter(
                x=[it["in_degree"] for it in nt_items],
                y=[it["out_degree"] for it in nt_items],
                mode="markers",
                name=nt,
                marker=dict(
                    size=sizes,
                    color=type_color[nt],
                    opacity=0.75,
                    line=dict(width=1, color="rgba(255,255,255,0.4)"),
                ),
                text=hover_texts,
                hoverinfo="text",
            )
        )

    fig.update_layout(
        **_base_layout(
            title="Node Centrality — In-Degree vs Out-Degree",
            height=480,
            xaxis=dict(title="In-Degree (incoming edges)"),
            yaxis=dict(title="Out-Degree (outgoing edges)"),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=-0.25,
                xanchor="center",
                x=0.5,
            ),
        )
    )
    st.plotly_chart(fig, use_container_width=True)

    # Top-10 high centrality nodes table
    top_n = items[:10]
    if top_n:
        st.markdown("**Top 10 High-Centrality Nodes**")
        top_rows = [
            {
                "Node": it.get("primary_value") or it["dedupe_key"][:30],
                "Type": it["node_type"],
                "In": it["in_degree"],
                "Out": it["out_degree"],
                "Total Degree": it["total_degree"],
            }
            for it in top_n
        ]
        st.dataframe(top_rows, use_container_width=True, hide_index=True)
