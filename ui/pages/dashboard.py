"""
ui/pages/dashboard.py — Tabbed run dashboard (SPEC-08 S-08.6).

Thin orchestrator that dispatches to per-tab renderer modules. All data
fetched from the backend — no business logic (CLAUDE.md §4.1, SKILL-B R-B3).

Tabs:
  0. Overview  — run summary, worker/cache, recent activity
  1. Schema    — locked domain schema (read-only)
  2. Ingestion — documents, parser tiers, chunk expanders, quality charts
  3. Extraction — progress gauge, per-chunk entity breakdown, model info
  4. Curation  — candidates, proposals, agent pipeline, telemetry, rejections
  5. Graph     — node/edge count charts (Plotly)
"""

import streamlit as st

from ui.components.dashboard_curation import render_curation_tab
from ui.components.dashboard_extraction import render_extraction_tab
from ui.components.dashboard_graph import render_graph_tab
from ui.components.dashboard_ingestion import render_ingestion_tab
from ui.components.dashboard_overview import render_overview_tab
from ui.components.dashboard_schema import render_schema_tab
from ui.components.phase_indicator import render_phase_indicator
from ui.state import StateManager


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

    tabs = st.tabs(["Overview", "Schema", "Ingestion", "Extraction", "Curation", "Graph"])

    with tabs[0]:
        render_overview_tab(run_id, state)
    with tabs[1]:
        render_schema_tab(run_id)
    with tabs[2]:
        render_ingestion_tab(run_id)
    with tabs[3]:
        render_extraction_tab(run_id)
    with tabs[4]:
        render_curation_tab(run_id)
    with tabs[5]:
        render_graph_tab(run_id)


if __name__ == "__main__":
    main()
