"""
ui/components/activity_feed.py — Sidebar activity feed widget.

Displays curated, user-relevant log entries (warnings, errors, key events)
from the backend ring buffer. Always visible in the sidebar regardless of
the current phase.

Architecture:
  - Fetches from GET /api/monitoring/logs/activity (curated filter).
  - Renders compact single-line entries with color-coded severity dots.
  - Fails gracefully — never blocks the sidebar on API errors.
  - No business logic (SKILL-B R-B3).
"""

import os
from datetime import UTC, datetime

import streamlit as st


_API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8000")


def _relative_time(iso_timestamp: str) -> str:
    """Convert an ISO 8601 timestamp to a relative string like '2m ago'."""
    try:
        ts = datetime.fromisoformat(iso_timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        delta = datetime.now(UTC) - ts
        seconds = int(delta.total_seconds())
        if seconds < 0:
            return "just now"
        if seconds < 60:
            return f"{seconds}s ago"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        return f"{days}d ago"
    except (ValueError, TypeError):
        return ""


def _severity_dot(level: str) -> str:
    """Return a colored markdown dot for the log level."""
    level_lower = level.lower()
    if level_lower in ("error", "critical"):
        return ":red[●]"
    if level_lower in ("warning", "warn"):
        return ":orange[●]"
    return ":blue[●]"


def render_activity_feed(run_id: str | None) -> None:
    """Render the activity feed inside a sidebar expander.

    Fetches curated log entries from the backend and displays them as
    compact single-line items with severity indicators and relative
    timestamps.

    Args:
        run_id: Current session run_id for scoping. May be None.
    """
    with st.expander("Activity", expanded=True):
        try:
            import httpx

            params: dict[str, str | int] = {"limit": 8}
            if run_id:
                params["run_id"] = run_id

            resp = httpx.Client(timeout=2.0).get(
                f"{_API_BASE_URL}/api/monitoring/logs/activity",
                params=params,
            )

            if resp.status_code != 200:
                st.caption(":gray[Activity unavailable]")
                return

            data = resp.json()
            entries = data.get("entries", [])

            if not entries:
                st.caption("No recent activity.")
                return

            for entry in entries:
                level = str(entry.get("level", "info"))
                event = str(entry.get("event", "unknown")).replace("_", " ")
                timestamp = entry.get("timestamp", "")
                rel_time = _relative_time(timestamp)

                dot = _severity_dot(level)
                line = f"{dot} {event}"
                if rel_time:
                    line += f"  :gray[{rel_time}]"
                st.markdown(line)

        except Exception:
            st.caption(":gray[Activity unavailable]")
