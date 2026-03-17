# components — Reusable Streamlit UI components

from ui.components.activity_feed import render_activity_feed as render_activity_feed
from ui.components.api_fetch import fetch as fetch
from ui.components.candidate_summary import (
    method_label as method_label,
    short_summary as short_summary,
    summarize_candidate as summarize_candidate,
)
from ui.components.monitoring_helpers import (
    format_duration as format_duration,
    render_entity_yield_metrics as render_entity_yield_metrics,
    render_failed_chunks_expander as render_failed_chunks_expander,
    render_worker_cache_row as render_worker_cache_row,
)
from ui.components.phase_indicator import render_phase_indicator as render_phase_indicator
from ui.components.plotly_helpers import (
    COLORS as COLORS,
    bar_chart as bar_chart,
    donut_chart as donut_chart,
    gauge_chart as gauge_chart,
    stacked_bar as stacked_bar,
)

# Dashboard tab renderers
from ui.components.dashboard_curation import render_curation_tab as render_curation_tab
from ui.components.dashboard_extraction import render_extraction_tab as render_extraction_tab
from ui.components.dashboard_graph import render_graph_tab as render_graph_tab
from ui.components.dashboard_ingestion import render_ingestion_tab as render_ingestion_tab
from ui.components.dashboard_overview import render_overview_tab as render_overview_tab
from ui.components.dashboard_schema import render_schema_tab as render_schema_tab
