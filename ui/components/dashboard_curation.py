"""
ui/components/dashboard_curation.py — Curation tab for the dashboard.

The most data-rich tab: candidate archive, proposal history, agent pipeline
progress, agent telemetry, and exclusions/rejections.

Architecture rules:
  - No business logic (CLAUDE.md §4.1, SKILL-B R-B3).
  - No st.session_state access outside StateManager (CLAUDE.md §8).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import streamlit as st

from ui.components.api_fetch import fetch
from ui.components.candidate_summary import method_label, summarize_candidate
from ui.components.plotly_helpers import bar_chart, donut_chart


def render_curation_tab(run_id: str) -> None:
    """Render the Curation tab content."""
    st.subheader("Curation")

    _render_candidate_archive(run_id)
    st.divider()
    _render_proposal_history(run_id)
    st.divider()
    _render_agent_pipeline(run_id)
    st.divider()
    _render_agent_telemetry(run_id)
    st.divider()
    _render_exclusions(run_id)


# ---------------------------------------------------------------------------
# 1. Candidate Archive
# ---------------------------------------------------------------------------


def _render_candidate_archive(run_id: str) -> None:
    st.markdown("### Candidate Archive")

    data = fetch(f"/api/curation/candidates/{run_id}")
    if data is None or data.get("status") == "error":
        st.info("No candidate data available yet.")
        return

    groups: list[dict[str, Any]] = data.get("groups", [])
    all_candidates = [c for g in groups for c in g.get("candidates", [])]

    if not all_candidates:
        st.info("No candidates generated yet.")
        return

    st.metric("Total Candidates", len(all_candidates))

    # Table
    rows = [
        {
            "ID": (c.get("candidate_id") or "")[:16] + "\u2026",
            "Type": c.get("candidate_type", ""),
            "Method": method_label(c.get("detection_method", "")),
            "Severity": c.get("severity", "\u2014"),
            "Summary": summarize_candidate(c),
        }
        for c in all_candidates
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    # Chart: candidates by type + severity
    type_counts: dict[str, int] = {}
    for c in all_candidates:
        ctype = c.get("candidate_type", "unknown")
        type_counts[ctype] = type_counts.get(ctype, 0) + 1

    if type_counts:
        fig = bar_chart(
            list(type_counts.keys()),
            list(type_counts.values()),
            "Candidates by Type",
        )
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# 2. Proposal History
# ---------------------------------------------------------------------------


def _render_proposal_history(run_id: str) -> None:
    st.markdown("### Proposal History")

    data = fetch(f"/api/curation/proposals/{run_id}")
    if data is None or data.get("status") == "error":
        st.info("No proposal data available yet.")
        return

    proposals: list[dict[str, Any]] = data.get("proposals", [])
    if not proposals:
        st.info("No proposals generated yet.")
        return

    # Table
    rows = [
        {
            "Proposal ID": (p.get("proposal_id") or "")[:16] + "\u2026",
            "Candidate ID": (p.get("candidate_id") or "")[:16] + "\u2026",
            "Class": p.get("proposal_class", ""),
            "State": p.get("state", ""),
            "Confidence": p.get("confidence_score", "\u2014"),
            "Rationale": (p.get("rationale") or "")[:80] + ("\u2026" if len(p.get("rationale") or "") > 80 else ""),
        }
        for p in proposals
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    # Donut: proposal state distribution
    state_counts: dict[str, int] = {}
    for p in proposals:
        s = p.get("state", "unknown")
        state_counts[s] = state_counts.get(s, 0) + 1

    if state_counts:
        fig = donut_chart(
            list(state_counts.keys()),
            list(state_counts.values()),
            "Proposals by State",
        )
        st.plotly_chart(fig, use_container_width=True)

    # Bar: proposals by class
    class_counts: dict[str, int] = {}
    for p in proposals:
        pc = p.get("proposal_class", "unknown")
        class_counts[pc] = class_counts.get(pc, 0) + 1

    if class_counts:
        fig = bar_chart(
            list(class_counts.keys()),
            list(class_counts.values()),
            "Proposals by Class",
        )
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# 3. Agent Pipeline Progress (auto-refresh)
# ---------------------------------------------------------------------------


@st.fragment(run_every=timedelta(seconds=5))
def _render_agent_pipeline(run_id: str) -> None:
    st.markdown("### Agent Pipeline")

    status_data = fetch(f"/api/curation/agents/status/{run_id}")
    if status_data is None or status_data.get("status") == "error":
        st.info("No agent pipeline data available yet.")
        return

    jobs: list[dict[str, Any]] = status_data.get("jobs", [])
    if not jobs:
        st.info("Agent pipeline has not started.")
        return

    total = len(jobs)
    completed = sum(1 for j in jobs if j.get("stage") == "complete")
    failed = sum(1 for j in jobs if j.get("stage") == "failed")
    deferred = sum(1 for j in jobs if j.get("stage") == "deferred")
    cancelled = sum(1 for j in jobs if j.get("stage") == "cancelled")
    finished = completed + failed + deferred + cancelled
    running = total - finished

    fraction = finished / total if total > 0 else 0.0
    st.progress(fraction, text=f"{finished} / {total} candidates processed")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Completed", completed)
    c2.metric("Running", running)
    c3.metric("Failed", failed)
    c4.metric("Deferred", deferred)

    # Per-stage bar
    stage_counts: dict[str, int] = {}
    for j in jobs:
        stage = j.get("stage", "unknown")
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    if stage_counts:
        fig = bar_chart(
            list(stage_counts.keys()),
            list(stage_counts.values()),
            "Candidates by Stage",
        )
        st.plotly_chart(fig, use_container_width=True)

    # Model info from agent config
    config = fetch("/api/curation/agents/config")
    if config and config.get("status") != "error":
        models: dict[str, str] = {}
        for agent_name in ("agent_a", "agent_b", "agent_p"):
            agent_cfg = config.get(agent_name, {})
            if isinstance(agent_cfg, dict):
                m = agent_cfg.get("model", "")
                if m:
                    models[agent_name.replace("_", "-").upper()] = m
        if models:
            st.markdown("**Agent Models**")
            for name, model in models.items():
                st.caption(f"{name}: `{model}`")


# ---------------------------------------------------------------------------
# 4. Agent Telemetry
# ---------------------------------------------------------------------------


def _render_agent_telemetry(run_id: str) -> None:
    st.markdown("### Agent Telemetry")

    data = fetch(f"/api/monitoring/agents/{run_id}")
    if data is None or data.get("status") == "error":
        st.info("No agent telemetry available yet.")
        return

    records: list[dict[str, Any]] = data.get("records", [])
    if not records:
        st.info("No telemetry records.")
        return

    rows = [
        {
            "Candidate ID": (r.get("candidate_id") or "")[:16] + "\u2026",
            "Agent": r.get("agent_name", ""),
            "Tokens In": r.get("tokens_in", 0),
            "Tokens Out": r.get("tokens_out", 0),
            "Cost ($)": f"{r.get('cost_estimate', 0):.4f}",
            "Time (ms)": r.get("execution_time_ms", 0),
        }
        for r in records
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    # Total tokens by agent
    agent_tokens: dict[str, int] = {}
    for r in records:
        name = r.get("agent_name", "unknown")
        total = r.get("tokens_in", 0) + r.get("tokens_out", 0)
        agent_tokens[name] = agent_tokens.get(name, 0) + total

    if agent_tokens:
        fig = bar_chart(
            list(agent_tokens.keys()),
            list(agent_tokens.values()),
            "Total Tokens by Agent",
        )
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# 5. Exclusions & Rejections
# ---------------------------------------------------------------------------


def _render_exclusions(run_id: str) -> None:
    st.markdown("### Exclusions & Rejections")

    data = fetch(f"/api/curation/proposals/{run_id}")
    if data is None or data.get("status") == "error":
        st.info("No proposal data available.")
        return

    proposals: list[dict[str, Any]] = data.get("proposals", [])
    excluded = [
        p for p in proposals if p.get("state") in ("rejected", "excluded", "deferred")
    ]

    if not excluded:
        st.info("No rejected, excluded, or deferred proposals.")
        return

    rows = [
        {
            "Proposal ID": (p.get("proposal_id") or "")[:16] + "\u2026",
            "State": p.get("state", ""),
            "Class": p.get("proposal_class", ""),
            "Rationale": p.get("rationale", "\u2014") or "\u2014",
        }
        for p in excluded
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)
