"""
ui/pages/monitoring_panels.py — SPEC-08 monitoring panels.

Extracted from ui/pages/monitoring.py (SKILL-B R-B7 refactor).
Contains panels added by SPEC-08 (Monitoring Polish, CI, Documentation):
  - Log export:    JSON download of all recent log entries
  - Cache panel:   hit/miss ratio, key count, memory usage
  - Alerting:      threshold-based warning badges
  - Metrics panel: per-agent token usage, response time percentiles

Architecture rules:
  - No business logic — pure data display (CLAUDE.md §4.1, SKILL-B R-B3).
  - All session state via StateManager.get() (CLAUDE.md §8).
  - Panels hide gracefully when backend endpoints are unreachable
    (SKILL-D R-D12).
"""

import json
from typing import Any

import streamlit as st


# ---------------------------------------------------------------------------
# Log export (SPEC-08 S-08.3)
# ---------------------------------------------------------------------------


def _render_log_export(data: dict[str, Any] | None) -> None:
    """Render a JSON download button for log entries.

    Hides when data is None (API unreachable). The full entries list is
    serialised to JSON with indentation for readability.
    """
    if data is None:
        return

    entries: list[dict[str, Any]] = data.get("entries", [])
    if not entries:
        return

    json_bytes = json.dumps(entries, indent=2, default=str).encode("utf-8")
    st.download_button(
        label=f"Export logs ({len(entries)} entries)",
        data=json_bytes,
        file_name="neo4all_logs.json",
        mime="application/json",
    )


# ---------------------------------------------------------------------------
# Cache dashboard panel (SPEC-08 S-08.3)
# ---------------------------------------------------------------------------


def _render_cache_panel(data: dict[str, Any] | None) -> None:
    """Render cache statistics from GET /api/monitoring/cache.

    Shows key count, memory usage, hit/miss counts, and a bar chart of
    hit vs miss proportions. Hides gracefully when data is None.
    """
    st.subheader("Cache Dashboard")

    if data is None:
        st.warning("Cannot reach API — cache data unavailable.")
        return

    total_keys: int = data.get("total_keys", 0)
    memory_human: str = data.get("memory_used_human", "0B")
    hit_count: int = data.get("hit_count", 0)
    miss_count: int = data.get("miss_count", 0)
    hit_ratio: float = data.get("hit_ratio", 0.0)

    col_k, col_m, col_r = st.columns(3)
    col_k.metric("Total Keys", total_keys, help="Keys in current Redis database (DBSIZE).")
    col_m.metric("Memory", memory_human, help="Redis used_memory (human-readable).")
    col_r.metric(
        "Hit Ratio",
        f"{hit_ratio * 100:.1f}%",
        help="Application-level cache hit ratio.",
    )

    # Bar chart: hits vs misses.
    if hit_count > 0 or miss_count > 0:
        import pandas as pd

        chart_df = pd.DataFrame(
            {"Count": [hit_count, miss_count]},
            index=["Hits", "Misses"],
        )
        st.bar_chart(chart_df)
    else:
        st.caption("No cache operations recorded yet.")


# ---------------------------------------------------------------------------
# Alerting thresholds panel (SPEC-08 S-08.3)
# ---------------------------------------------------------------------------


def _render_alerting_panel(
    *,
    logs_data: dict[str, Any] | None,
    workers_data: dict[str, Any] | None,
    cache_data: dict[str, Any] | None,
    error_rate_threshold: float,
    queue_depth_threshold: int,
    cache_ratio_threshold: float,
) -> None:
    """Render alerting badges when current values exceed thresholds.

    All data comes from API responses — no business logic (SKILL-B R-B3).
    Computes simple ratios for display purposes only.
    """
    alerts: list[str] = []

    # Error rate: ERROR + CRITICAL entries as % of total log entries.
    if logs_data is not None:
        entries: list[dict[str, Any]] = logs_data.get("entries", [])
        total = len(entries)
        if total > 0:
            error_count = sum(
                1
                for e in entries
                if str(e.get("level", "")).upper() in ("ERROR", "CRITICAL")
            )
            error_rate = (error_count / total) * 100
            if error_rate >= error_rate_threshold:
                alerts.append(
                    f":red[Error rate {error_rate:.1f}% >= {error_rate_threshold:.0f}% threshold]"
                )

    # Queue depth.
    if workers_data is not None and workers_data.get("status") != "error":
        queue_depth: int = workers_data.get("queue_depth", 0)
        if queue_depth >= queue_depth_threshold:
            alerts.append(
                f":red[Queue depth {queue_depth} >= {queue_depth_threshold} threshold]"
            )

    # Cache hit ratio (warn when BELOW threshold).
    if cache_data is not None:
        hit_ratio: float = cache_data.get("hit_ratio", 0.0)
        total_ops = cache_data.get("hit_count", 0) + cache_data.get("miss_count", 0)
        if total_ops > 0 and (hit_ratio * 100) < cache_ratio_threshold:
            alerts.append(
                f":orange[Cache hit ratio {hit_ratio * 100:.1f}% < "
                f"{cache_ratio_threshold:.0f}% threshold]"
            )

    if alerts:
        st.subheader("Alerts")
        for alert in alerts:
            st.markdown(alert)
    else:
        st.caption(":green[All metrics within thresholds.]")


# ---------------------------------------------------------------------------
# Historical metrics panel (SPEC-08 S-08.3)
# ---------------------------------------------------------------------------


def _render_metrics_panel(data: dict[str, Any] | None) -> None:
    """Render aggregated LLM metrics and response time percentiles.

    Data from GET /api/monitoring/metrics. Shows per-agent token usage,
    invocation counts, and response time percentile distribution.
    """
    st.subheader("LLM Usage & Performance")

    if data is None:
        st.warning("Cannot reach API — metrics data unavailable.")
        return

    agents: list[dict[str, Any]] = data.get("agents", [])
    total_candidates: int = data.get("total_candidates_processed", 0)

    st.caption(f"Candidates processed: **{total_candidates}**")

    if agents:
        import pandas as pd

        rows = [
            {
                "Agent": a.get("agent_name", ""),
                "Tokens In": a.get("total_tokens_in", 0),
                "Tokens Out": a.get("total_tokens_out", 0),
                "Cost ($)": f"{a.get('total_cost', 0.0):.4f}",
                "Invocations": a.get("invocation_count", 0),
            }
            for a in agents
        ]
        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No agent invocations recorded yet.")

    # Response time percentiles.
    rt = data.get("response_time")
    if rt:
        st.caption("Response Time Percentiles (ms)")
        col_p50, col_p95, col_p99 = st.columns(3)
        col_p50.metric("p50", f"{rt.get('p50', 0.0):.1f}")
        col_p95.metric("p95", f"{rt.get('p95', 0.0):.1f}")
        col_p99.metric("p99", f"{rt.get('p99', 0.0):.1f}")
