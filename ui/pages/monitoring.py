"""
ui/pages/monitoring.py — Monitoring dashboard (SKILL-D R-D12).

Read-only page. Polls backend monitoring endpoints and renders:
  - Backend panel: per-service health with latency (GET /api/monitoring/health)
  - Log viewer:    recent log entries, filterable by level
                   (GET /api/monitoring/logs/recent)
  - Worker panel:  ARQ queue depth, active jobs, worker count
                   (GET /api/monitoring/workers)  [SPEC-04]
  - Job tracker:   per-chunk extraction job statuses in an expander
                   (GET /api/monitoring/jobs/{run_id})  [SPEC-04]
  - Session panel: frontend phase state and timings from StateManager
  - Auto-refresh:  configurable poll interval, default 5 seconds

SPEC-08 panels (log export, cache dashboard, alerting, metrics) are in
ui/pages/monitoring_panels.py — imported and called from main().

Architecture rules:
  - No business logic — pure data display (CLAUDE.md §4.1, SKILL-B R-B3).
  - All session state via StateManager.get() (CLAUDE.md §8).
  - No st.session_state access outside StateManager.
  - API calls via httpx (synchronous client — Streamlit is single-threaded).
  - Panels hide gracefully when backend endpoints are unreachable
    (SKILL-D R-D12: "UI panels gracefully hide sections whose backend
    endpoints are not yet implemented").
  - No direct Redis access in UI — all worker/job data fetched via API.

Backend endpoints consumed:
  SPEC-01: GET /api/monitoring/health
           GET /api/monitoring/logs/recent?level=<level>&limit=<n>
           GET /api/monitoring/run/{run_id}
  SPEC-04: GET /api/monitoring/workers
           GET /api/monitoring/jobs/{run_id}
  SPEC-08: GET /api/monitoring/cache    (via monitoring_panels)
           GET /api/monitoring/metrics  (via monitoring_panels)
"""

import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import streamlit as st

from api.models.run import Phase
from ui.pages.monitoring_panels import (
    _render_alerting_panel,
    _render_cache_panel,
    _render_log_export,
    _render_metrics_panel,
)
from ui.state import StateManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8000")
_REQUEST_TIMEOUT: float = 5.0  # seconds per endpoint fetch
_DEFAULT_REFRESH_INTERVAL: int = 5  # seconds

_LEVEL_OPTIONS = ["ALL", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_DEFAULT_LEVEL_INDEX = 3  # "WARNING"

# Colour tokens used with Streamlit's markdown colour syntax.
_LEVEL_COLOURS: dict[str, str] = {
    "DEBUG": "gray",
    "INFO": "blue",
    "WARNING": "orange",
    "ERROR": "red",
    "CRITICAL": "red",
}

# Alerting threshold defaults (SPEC-08 S-08.3).
_DEFAULT_ERROR_RATE_THRESHOLD: float = 10.0   # percent
_DEFAULT_QUEUE_DEPTH_THRESHOLD: int = 50
_DEFAULT_CACHE_HIT_RATIO_THRESHOLD: float = 50.0  # percent (warn if BELOW)


# ---------------------------------------------------------------------------
# Backend fetch helpers — all errors return None, never raise (SKILL-D R-D9)
# ---------------------------------------------------------------------------


def _fetch(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """GET {_API_BASE_URL}{path} and return parsed JSON.

    Uses a short-lived synchronous httpx client. Returns None on any error
    (connection refused, timeout, non-2xx) so callers can hide the panel
    gracefully without crashing the page.

    Args:
        path:   URL path to append to API_BASE_URL (must start with "/").
        params: Optional query parameters dict.

    Returns:
        Parsed JSON dict on success, None on any failure.
    """
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            response = client.get(f"{_API_BASE_URL}{path}", params=params)
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Panel renderers — pure presentation, no state mutations
# ---------------------------------------------------------------------------


def _render_health_panel(data: dict[str, Any] | None) -> None:
    """Render per-service health indicators.

    Hides gracefully when data is None (API unreachable).
    """
    st.subheader("Backend Services")

    if data is None:
        st.warning("Cannot reach API — health data unavailable.")
        return

    services: list[dict[str, Any]] = data.get("services", [])
    if not services:
        st.info("No service data returned.")
        return

    for svc in services:
        name: str = svc.get("name", "unknown")
        healthy: bool = svc.get("healthy", False)
        latency_ms: float | None = svc.get("latency_ms")
        error: str | None = svc.get("error")

        colour = "green" if healthy else "red"
        dot = f":{colour}[●]"

        col_dot, col_name, col_latency = st.columns([1, 3, 2])
        with col_dot:
            st.markdown(dot)
        with col_name:
            st.write(name)
        with col_latency:
            if latency_ms is not None:
                st.caption(f"{latency_ms} ms")
            elif not healthy:
                st.caption("—")

        if error:
            st.caption(f":orange[{error}]")


def _render_log_viewer(
    data: dict[str, Any] | None,
    selected_level: str,
) -> None:
    """Render recent log entries from the ring buffer.

    Hides gracefully when data is None (API unreachable).

    Args:
        data:           Parsed response from /api/monitoring/logs/recent.
        selected_level: The level filter label shown to the user ("ALL", etc.).
    """
    label = "Recent Logs" + (f" (≥ {selected_level})" if selected_level != "ALL" else "")
    st.subheader(label)

    if data is None:
        st.warning("Cannot reach API — log data unavailable.")
        return

    entries: list[dict[str, Any]] = data.get("entries", [])
    total: int = data.get("total", len(entries))

    if not entries:
        st.success("No log entries match the current filter.")
        return

    st.caption(f"Showing {len(entries)} of {total} matching entries (newest first)")

    for entry in entries:
        level_raw: str = str(entry.get("level", "INFO")).upper()
        event: str = str(entry.get("event", ""))
        timestamp: str = str(entry.get("timestamp", ""))
        component: str = str(entry.get("component", ""))
        correlation_id: str = str(entry.get("correlation_id", ""))

        # Shorten the ISO timestamp to HH:MM:SS.
        short_ts = timestamp[11:19] if len(timestamp) >= 19 else timestamp

        colour = _LEVEL_COLOURS.get(level_raw, "gray")
        level_badge = f":{colour}[**{level_raw}**]"

        parts = [level_badge, f"`{short_ts}`", f"**{event}**"]
        if component:
            parts.append(f"_{component}_")
        if correlation_id:
            parts.append(f"id:`{correlation_id[:8]}`")

        st.markdown(" ".join(parts))


def _render_session_panel(state: StateManager) -> None:
    """Render frontend session state and phase timings from StateManager.

    Reads exclusively from StateManager — no API calls made here.
    This panel is always available regardless of backend connectivity.
    """
    st.subheader("Session State")

    current_phase = state.phase
    run_id = state.run_id
    schema_version = state.schema_version
    phase_times = state.phase_start_times

    col_left, col_right = st.columns(2)

    with col_left:
        st.metric("Current Phase", f"{current_phase.value}: {current_phase.name.title()}")

        if run_id:
            st.caption("Run ID")
            st.code(run_id, language=None)
        else:
            st.info("No active run. Initialize a session in Phase 0.")

        if schema_version:
            st.caption("Schema Version")
            st.write(schema_version)

    with col_right:
        st.caption("Phase Timings")
        now = datetime.now(UTC)

        for p in Phase:
            iso = phase_times.get(p.value)
            if iso is None:
                # Phase not yet reached — show as pending.
                if p.value > current_phase.value:
                    st.write(f":gray[○] **{p.name}**: pending")
                continue

            start = datetime.fromisoformat(iso)
            elapsed = now - start

            if p == current_phase:
                st.write(
                    f":blue[→] **{p.name}**: active — {_fmt_duration(elapsed)} elapsed"
                )
            else:
                st.write(f":green[✓] **{p.name}**: {iso[11:19]} UTC")


# ---------------------------------------------------------------------------
# Worker monitoring panels (SPEC-04)
# ---------------------------------------------------------------------------


def _render_worker_panel(data: dict[str, Any] | None) -> None:
    """Render ARQ worker status from GET /api/monitoring/workers.

    Hides gracefully when data is None (API unreachable or endpoint not yet
    available). Displays queue depth, active jobs, and worker count as metrics.
    No business logic — pure display (SKILL-B R-B3).
    """
    st.subheader("Worker Status")

    if data is None:
        st.warning("Cannot reach API — worker data unavailable.")
        return

    if data.get("status") == "error":
        for e in data.get("errors", []):
            st.caption(f":orange[{e.get('message', 'Worker status unavailable')}]")
        return

    queue_depth: int = data.get("queue_depth", 0)
    active_jobs: int = data.get("active_jobs", 0)
    worker_count: int = data.get("worker_count", 0)

    col_q, col_a, col_w = st.columns(3)
    col_q.metric("Queue Depth", queue_depth, help="Jobs waiting in the ARQ sorted-set queue.")
    col_a.metric("Active Jobs", active_jobs, help="Chunk jobs currently in 'running' state.")
    col_w.metric("Workers", worker_count, help="Live ARQ worker processes (health-check keys).")

    if worker_count == 0:
        st.caption(
            ":orange[No workers connected. Start the ARQ worker process to "
            "process extraction jobs.]"
        )
    elif queue_depth == 0 and active_jobs == 0:
        st.caption(":green[Workers idle — all queued jobs are complete.]")


def _render_job_tracker(data: dict[str, Any] | None, run_id: str = "") -> None:
    """Render per-chunk extraction job statuses for the current run.

    Called with data from GET /api/monitoring/jobs/{run_id}. Hidden when data
    is None. Uses st.expander so the tracker is collapsed by default for runs
    with many chunks (> 20). Renders at most 50 rows to avoid overwhelming the
    UI. Failed chunks get expandable detail panels with chunk text retrieval.
    No business logic — pure display (SKILL-B R-B3).
    """
    total: int = data.get("total", 0) if data else 0
    label = f"Extraction Job Tracker — {total} chunk(s)"

    with st.expander(label, expanded=(0 < total <= 20)):
        if data is None:
            st.warning("Cannot reach API — job data unavailable.")
            return

        if data.get("status") == "error":
            for e in data.get("errors", []):
                st.caption(f":orange[{e.get('message', 'Job data unavailable')}]")
            return

        jobs: list[dict[str, Any]] = data.get("jobs", [])
        if not jobs:
            st.info("No extraction jobs found for this run. Start extraction in Phase 3.")
            return

        completed: int = data.get("completed", 0)
        failed: int = data.get("failed", 0)
        pending: int = data.get("pending", 0)

        col_c, col_f, col_p = st.columns(3)
        col_c.metric("Complete", completed)
        col_f.metric("Failed", failed)
        col_p.metric("Pending", pending)

        st.divider()

        display_jobs = jobs[:50]
        if len(jobs) > 50:
            st.caption(f"Showing 50 of {len(jobs)} chunks (oldest first).")

        rows = [
            {
                "Status": j.get("status", "unknown"),
                "Chunk": (j.get("chunk_id") or "")[:12] + "…",
                "Doc": (j.get("doc_id") or "")[:12] + "…",
                "Nodes": j.get("nodes_created", 0),
                "Edges": j.get("edges_created", 0),
                "Started": (j.get("started_at") or "")[ 11:19],
                "Error": (j.get("error") or "")[:40],
            }
            for j in display_jobs
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)

        # --- Expandable details for failed chunks ---
        failed_jobs = [j for j in display_jobs if j.get("status") == "failed"]
        if failed_jobs:
            st.caption(f"{len(failed_jobs)} failed chunk(s) — expand to inspect:")
            for j in failed_jobs:
                chunk_id = j.get("chunk_id") or "unknown"
                error_msg = j.get("error") or "No error message"
                with st.expander(f"Failed: {chunk_id[:16]}… — {error_msg[:60]}"):
                    st.code(f"Chunk ID: {chunk_id}\nDoc ID:   {j.get('doc_id', '')}\n\nError:\n{error_msg}")
                    if run_id and chunk_id != "unknown":
                        text_resp = _fetch(
                            f"/api/documents/{run_id}/chunk/{chunk_id}/text",
                        )
                        if text_resp and text_resp.get("status") == "success":
                            st.markdown("**Chunk text:**")
                            st.text(text_resp.get("text", ""))
                        else:
                            st.caption(":orange[Chunk text not available.]")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _fmt_duration(d: timedelta) -> str:
    """Format a timedelta as a concise human-readable string."""
    total_seconds = int(d.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Monitoring page entry point, called by Streamlit on every script rerun."""
    # NOTE: st.set_page_config() is called by app.py before routing here.
    # Calling it again raises StreamlitAPIException in Streamlit 1.40+.

    # All session state access through StateManager (CLAUDE.md §8).
    state = StateManager.get()

    # --- Sidebar: controls and API config ---
    with st.sidebar:
        st.header("Monitoring Controls")
        auto_refresh = st.toggle("Auto-refresh", value=True)
        refresh_interval = st.slider(
            "Interval (s)",
            min_value=2,
            max_value=30,
            value=_DEFAULT_REFRESH_INTERVAL,
            step=1,
        )
        st.divider()
        level_choice = st.selectbox(
            "Log level filter",
            options=_LEVEL_OPTIONS,
            index=_DEFAULT_LEVEL_INDEX,
        )
        log_limit = st.slider("Max log entries", min_value=10, max_value=100, value=20, step=10)
        st.divider()

        # Alerting thresholds (SPEC-08 S-08.3).
        st.subheader("Alert Thresholds")
        error_rate_threshold = st.number_input(
            "Error rate % (warn if >=)",
            min_value=0.0,
            max_value=100.0,
            value=_DEFAULT_ERROR_RATE_THRESHOLD,
            step=5.0,
        )
        queue_depth_threshold = st.number_input(
            "Queue depth (warn if >=)",
            min_value=0,
            max_value=1000,
            value=_DEFAULT_QUEUE_DEPTH_THRESHOLD,
            step=10,
        )
        cache_ratio_threshold = st.number_input(
            "Cache hit % (warn if below)",
            min_value=0.0,
            max_value=100.0,
            value=_DEFAULT_CACHE_HIT_RATIO_THRESHOLD,
            step=5.0,
        )
        st.divider()
        st.caption(f"API: `{_API_BASE_URL}`")

    # --- Page header ---
    st.title("Monitoring Dashboard")
    now_str = datetime.now(UTC).strftime("%H:%M:%S UTC")
    header_col, refresh_col = st.columns([6, 1])
    with header_col:
        st.caption(f"Last updated: {now_str}")
    with refresh_col:
        if not auto_refresh:
            if st.button("Refresh"):
                st.rerun()

    st.divider()

    # --- Fetch backend data (all calls wrapped, None on failure) ---
    health_data = _fetch("/api/monitoring/health")

    level_param: str | None = None if level_choice == "ALL" else level_choice
    log_params: dict[str, Any] = {"limit": log_limit}
    if level_param is not None:
        log_params["level"] = level_param
    logs_data = _fetch("/api/monitoring/logs/recent", params=log_params)

    run_id = state.run_id
    run_summary_data: dict[str, Any] | None = None
    if run_id:
        run_summary_data = _fetch(f"/api/monitoring/run/{run_id}")

    # SPEC-04: worker status and per-run job tracking
    workers_data = _fetch("/api/monitoring/workers")
    jobs_data: dict[str, Any] | None = None
    if run_id:
        jobs_data = _fetch(f"/api/monitoring/jobs/{run_id}")

    # SPEC-08: cache stats and aggregated metrics
    cache_data = _fetch("/api/monitoring/cache")
    metrics_data = _fetch("/api/monitoring/metrics")

    # --- Layout: two-column backend panels ---
    col_health, col_logs = st.columns(2)

    with col_health:
        _render_health_panel(health_data)

        # Server-side run summary — hides when not found (SPEC-01: always
        # found=False until a server-side run registry is introduced).
        if run_summary_data and run_summary_data.get("found"):
            st.divider()
            st.subheader("Run Summary (Server)")
            st.write(f"Phase: {run_summary_data.get('phase', 'unknown')}")
            st.write(f"Schema: {run_summary_data.get('schema_version') or 'not locked'}")

    with col_logs:
        _render_log_viewer(logs_data, selected_level=level_choice)
        _render_log_export(logs_data)

    st.divider()

    # --- Worker status (left) + session panel (right) ---
    col_worker, col_session = st.columns(2)

    with col_worker:
        _render_worker_panel(workers_data)

    with col_session:
        _render_session_panel(state)

    # --- Per-run job tracker (full-width, collapsible) ---
    if run_id:
        st.divider()
        _render_job_tracker(jobs_data, run_id=run_id)

    # --- SPEC-08: Cache dashboard + alerting + metrics ---
    st.divider()

    col_cache, col_alerts = st.columns(2)
    with col_cache:
        _render_cache_panel(cache_data)
    with col_alerts:
        _render_alerting_panel(
            logs_data=logs_data,
            workers_data=workers_data,
            cache_data=cache_data,
            error_rate_threshold=error_rate_threshold,
            queue_depth_threshold=queue_depth_threshold,
            cache_ratio_threshold=cache_ratio_threshold,
        )

    st.divider()
    _render_metrics_panel(metrics_data)

    # --- Auto-refresh: sleep then rerun (SKILL-D R-D12) ---
    # time.sleep() blocks this Streamlit connection for the interval.
    # Acceptable in a single-user dev environment. For multi-user production,
    # replace with a JS-based auto-refresh component.
    if auto_refresh:
        time.sleep(refresh_interval)
        st.rerun()


if __name__ == "__main__":
    main()
