"""
ui/pages/curation_agents.py — Layer 3 AI Agent Pipeline UI (SPEC-07).

Extracted from curation_pipeline.py per SKILL-B R-B7 (>400 line split).

Layer 3 (SPEC-07): AI agent pipeline with per-agent model selection.
  Model configuration -> trigger pipeline -> monitor progress.

Architecture rules:
  - No business logic — pure API calls and display (CLAUDE.md §4.1, SKILL-B).
  - All session state via StateManager.get() — no direct st.session_state
    access anywhere in this file (CLAUDE.md §8).
  - API calls via httpx synchronous client (Streamlit is single-threaded).
  - Panels degrade gracefully when the API is unreachable (SKILL-D R-D12).

Backend endpoints consumed:
  GET  /api/curation/agents/config
  POST /api/curation/agents/run
  POST /api/curation/agents/cancel
  GET  /api/curation/agents/status/{run_id}
  GET  /api/monitoring/agents/{run_id}
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import streamlit as st

from ui.components.monitoring_helpers import format_duration
from ui.pages.curation import (
    _fetch,
    _post,
)


# ===========================================================================
# Layer 3 — AI Agent Pipeline (SPEC-07)
# ===========================================================================


def _render_agent_model_config(run_id: str) -> None:
    """Render per-agent model selection and pipeline trigger.

    Fetches current defaults from GET /agents/config, lets the user override
    models per agent, and triggers the pipeline via POST /agents/run.
    Shows agent pipeline progress from GET /monitoring/agents/{run_id}.
    """
    st.subheader("AI Agent Pipeline")
    st.caption(
        "Layer 3: Run the AI curation agent chain (Agent-A -> Agent-B -> Agent-P) "
        "on detected candidates.  Each agent can use a different LLM model.  "
        "All AI proposals enter the same governed approval queue as manual proposals."
    )
    st.info(
        "**Minimum context window: 128k tokens.** "
        "The agent pipeline works best with frontier models that support at least "
        "128k token context windows (e.g. GPT-4o, Claude 3.5+, Gemini 1.5 Pro).",
        icon="\u2139\ufe0f",
    )

    # Fetch current config defaults.
    config_data = _fetch("/api/curation/agents/config")

    if config_data is None:
        st.warning("Cannot reach the agent config endpoint — model defaults unavailable.")
        default_a = "openrouter/hunter-alpha"
        default_b = "openrouter/hunter-alpha"
        default_p = "openrouter/hunter-alpha"
    else:
        default_a = config_data.get("agent_a", {}).get("model", "openrouter/hunter-alpha")
        default_b = config_data.get("agent_b", {}).get("model", "openrouter/hunter-alpha")
        default_p = config_data.get("agent_p", {}).get("model", "openrouter/hunter-alpha")

    st.markdown("**Per-Agent Model Assignment**")
    st.caption(
        "Override the OpenRouter model for each agent, or keep the defaults.  "
        "Changes apply only to this pipeline run — they do not modify the server config."
    )

    col_a, col_b, col_p = st.columns(3)
    with col_a:
        model_a = st.text_input(
            "Agent-A (Evidence Assembly)",
            value=default_a,
            key="agent_model_a",
            help="LLM model for evidence classification and sufficiency scoring.",
        )
    with col_b:
        model_b = st.text_input(
            "Agent-B (Retrieval Augmentation)",
            value=default_b,
            key="agent_model_b",
            help="LLM model for targeted retrieval when evidence is insufficient.",
        )
    with col_p:
        model_p = st.text_input(
            "Agent-P (Proposal Composer)",
            value=default_p,
            key="agent_model_p",
            help="LLM model for composing the Proposal Packet.",
        )

    # Show token budget info if config available.
    if config_data is not None:
        with st.expander("Token budgets & limits", expanded=False):
            budget_cols = st.columns(3)
            for i, (label, agent_key) in enumerate([
                ("Agent-A", "agent_a"),
                ("Agent-B", "agent_b"),
                ("Agent-P", "agent_p"),
            ]):
                cfg = config_data.get(agent_key, {})
                budget_cols[i].caption(
                    f"**{label}**: "
                    f"{cfg.get('max_input_tokens', 0):,} in / "
                    f"{cfg.get('max_output_tokens', 0):,} out tokens"
                )
            cost_limit = config_data.get("cost_limit_per_candidate", 0.0)
            max_rounds = config_data.get("max_retrieval_rounds", 0)
            st.caption(
                f"Cost limit: ${cost_limit:.2f}/candidate  |  "
                f"Max retrieval rounds: {max_rounds}"
            )

    # Agent-B retrieval toggle — defaults to server config.
    enable_retrieval_default = (
        config_data.get("enable_agent_b", False) if config_data else False
    )
    enable_retrieval = st.checkbox(
        "Enable retrieval augmentation",
        value=enable_retrieval_default,
        key="enable_retrieval_toggle",
        help=(
            "When checked, candidates with insufficient evidence are routed "
            "through Agent-B for additional retrieval before proposal composition."
        ),
    )

    st.markdown("---")

    # Check if pipeline is actively running to disable the trigger button.
    # Jobs are considered active only if they are in a running/queued stage
    # AND were updated recently (within 5 minutes).  Stale jobs from a
    # crashed or restarted worker do not block re-triggering.
    status_data = _fetch(f"/api/curation/agents/status/{run_id}")
    pipeline_jobs: list[dict[str, Any]] = (
        status_data.get("jobs", []) if status_data else []
    )
    _active_stages = {"queued", "evidence_running", "retrieval_running", "proposal_running"}
    _STALE_SECONDS = 300  # 5 minutes

    def _is_actively_running(job: dict[str, Any]) -> bool:
        if job.get("stage", "") not in _active_stages:
            return False
        updated = job.get("updated_at")
        if not updated:
            return False
        try:
            ts = datetime.fromisoformat(updated)
            age = (datetime.now(UTC) - ts).total_seconds()
            return age < _STALE_SECONDS
        except (ValueError, TypeError):
            return False

    pipeline_busy = bool(pipeline_jobs) and any(
        _is_actively_running(j) for j in pipeline_jobs
    )

    # Stop button — shown when pipeline is actively running.
    if pipeline_busy:
        @st.dialog("Stop Agent Pipeline")
        def _confirm_stop_pipeline() -> None:
            st.warning(
                "Are you sure? This will cancel all running and queued "
                "agent pipeline jobs. Already-complete proposals are preserved."
            )
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Confirm", type="primary", key="confirm_stop_pipeline"):
                    _post("/api/curation/agents/cancel", {"run_id": run_id})
                    st.rerun()
            with col2:
                if st.button("Keep Running", key="keep_pipeline_running"):
                    st.rerun()

        if st.button(
            "Stop Pipeline", type="secondary", key="stop_agent_pipeline"
        ):
            _confirm_stop_pipeline()

    # Collect candidate IDs from all loaded candidate groups for targeted pipeline runs.
    # When the UI has loaded candidates (from any stage), pass their IDs so the
    # pipeline processes only the currently visible candidates.
    _all_candidate_ids: list[str] = []
    candidates_data = _fetch(f"/api/curation/candidates/{run_id}")
    if candidates_data and candidates_data.get("status") != "error":
        for g in candidates_data.get("groups", []):
            for c in g.get("candidates", []):
                cid = c.get("candidate_id", "")
                if cid:
                    _all_candidate_ids.append(cid)

    # Build the base payload for pipeline trigger calls.
    def _build_pipeline_payload(confirm_archive: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": run_id,
            "model_a": model_a.strip() or None,
            "model_b": model_b.strip() or None,
            "model_p": model_p.strip() or None,
            "confirm_archive": confirm_archive,
            "enable_retrieval": enable_retrieval,
        }
        if _all_candidate_ids:
            payload["candidate_ids"] = _all_candidate_ids
        return payload

    def _handle_pipeline_response(data: dict[str, Any] | None, err: str | None) -> None:
        """Display success/error for a pipeline trigger response."""
        if err or data is None:
            st.error(f"Pipeline trigger failed: {err or 'No response from API.'}")
        elif data.get("status") == "error":
            for e in data.get("errors", []):
                st.error(f"[{e.get('code')}] {e.get('message')}")
        else:
            enqueued = data.get("jobs_enqueued", 0)
            batch_size = data.get("batch_size", 1)
            if batch_size > 1:
                jobs_label = (
                    f"{enqueued} batch job(s) "
                    f"(up to {batch_size} candidates each)"
                )
            else:
                jobs_label = f"{enqueued} job(s)"
            st.success(
                f"Pipeline started — {jobs_label} enqueued.  "
                f"Models: A={data.get('model_a', '?')}, "
                f"B={data.get('model_b', '?')}, "
                f"P={data.get('model_p', '?')}"
            )

    @st.dialog("Archive Previous Proposals")
    def _confirm_archive_proposals(proposal_count: int) -> None:
        st.warning(
            f"There are **{proposal_count}** existing proposal(s) from a previous "
            "pipeline run. These will be archived before the new pipeline starts."
        )
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Confirm & Run", type="primary", key="confirm_archive_run"):
                with st.spinner("Archiving proposals and starting pipeline..."):
                    data, err = _post(
                        "/api/curation/agents/run",
                        _build_pipeline_payload(confirm_archive=True),
                    )
                _handle_pipeline_response(data, err)
                st.rerun()
        with col2:
            if st.button("Cancel", key="cancel_archive"):
                st.rerun()

    # Trigger button.
    if st.button(
        "Curate",
        type="primary",
        key="run_agent_pipeline",
        disabled=pipeline_busy,
        help=(
            "Enqueues all detected candidates for processing by the AI agent chain. "
            "Progress is shown below and in the Dashboard."
        )
        if not pipeline_busy
        else "Pipeline is already running — wait for it to finish.",
    ):
        with st.spinner("Enqueuing agent pipeline jobs..."):
            data, err = _post(
                "/api/curation/agents/run",
                _build_pipeline_payload(confirm_archive=False),
            )

        if data is not None and data.get("pending_archive"):
            _confirm_archive_proposals(data.get("existing_proposal_count", 0))
        else:
            _handle_pipeline_response(data, err)

    # Agent pipeline progress — fragment fetches its own data on each rerun.
    _agent_pipeline_fragment(run_id)


@st.fragment(run_every=timedelta(seconds=3))
def _agent_pipeline_fragment(run_id: str) -> None:
    """Fragment wrapper around agent pipeline progress.

    Auto-refreshes every 3 s so the user sees live progress while the
    agent pipeline is running.  Only this fragment re-renders — the rest
    of the page stays stable.
    """
    _render_agent_pipeline_progress(run_id)


def _render_agent_pipeline_progress(run_id: str) -> None:
    """Fetch and display per-candidate agent pipeline status."""
    st.markdown("**Agent Pipeline Progress**")

    data = _fetch(f"/api/curation/agents/status/{run_id}")

    if data is None:
        st.caption("No agent pipeline telemetry available.")
        return

    if data.get("status") == "error":
        for e in data.get("errors", []):
            st.error(f"[{e.get('code')}] {e.get('message')}")
        return

    jobs: list[dict[str, Any]] = data.get("jobs", [])
    if not jobs:
        st.caption("No agent jobs found for this run. Trigger the pipeline above.")
        return

    # Summary metrics.
    total = len(jobs)
    completed = sum(1 for j in jobs if j.get("stage") == "complete")
    failed = sum(1 for j in jobs if j.get("stage") == "failed")
    deferred = sum(1 for j in jobs if j.get("stage") == "deferred")
    cancelled = sum(1 for j in jobs if j.get("stage") == "cancelled")
    running = total - completed - failed - deferred - cancelled
    proposals_made = sum(1 for j in jobs if j.get("proposal_id"))

    # Elapsed time: earliest started_at to latest updated_at.
    started_timestamps: list[str] = [
        j["started_at"] for j in jobs if j.get("started_at")
    ]
    updated_timestamps: list[str] = [
        j["updated_at"] for j in jobs if j.get("updated_at")
    ]
    elapsed_str = ""
    if started_timestamps and updated_timestamps:
        earliest = min(started_timestamps)
        latest = max(updated_timestamps)
        elapsed_str = format_duration(earliest, latest)

    # Progress bar.
    finished = completed + failed + deferred + cancelled
    fraction = finished / total if total > 0 else 0.0
    progress_text = f"Progress: {finished} / {total} candidates processed"
    if elapsed_str:
        if running > 0:
            progress_text += f"  \u2014  Pipeline running for {elapsed_str}"
        else:
            progress_text += f"  \u2014  Completed in {elapsed_str}"
    st.progress(fraction, text=progress_text)

    # Compact metric cards — now includes Cancelled.
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Completed", f"{completed}/{total}")
    c2.metric("Running", running)
    c3.metric("Failed", failed)
    c4.metric("Deferred", deferred)
    c5.metric("Cancelled", cancelled)
    c6.metric("Proposals", proposals_made)

    # Cost/token summary from agent telemetry.
    telemetry = _fetch(f"/api/monitoring/agents/{run_id}")
    if telemetry is not None and telemetry.get("status") != "error":
        records: list[dict[str, Any]] = telemetry.get("records", [])
        if records:
            total_input_tokens = sum(r.get("input_tokens", 0) for r in records)
            total_output_tokens = sum(r.get("output_tokens", 0) for r in records)
            total_cost = sum(r.get("cost", 0.0) for r in records)
            tc1, tc2, tc3 = st.columns(3)
            tc1.metric("Tokens in", f"{total_input_tokens:,}")
            tc2.metric("Tokens out", f"{total_output_tokens:,}")
            tc3.metric("Est. cost", f"${total_cost:.4f}")

    # Stage breakdown — one-line summary when jobs are actively running.
    if running > 0:
        _running_stages: dict[str, int] = {}
        _stage_labels: dict[str, str] = {
            "queued": "Queued",
            "evidence_running": "Analyzing evidence",
            "evidence_complete": "Evidence done",
            "retrieval_running": "Retrieving",
            "retrieval_complete": "Retrieval done",
            "proposal_running": "Composing proposals",
        }
        for j in jobs:
            stage = j.get("stage", "")
            if stage in _stage_labels:
                label = _stage_labels[stage]
                _running_stages[label] = _running_stages.get(label, 0) + 1
        if _running_stages:
            parts = [f"{label}: **{count}**" for label, count in _running_stages.items()]
            st.caption(" | ".join(parts))

    # Live-update indicator.
    if running > 0:
        st.caption(":blue[Pipeline running — auto-refreshes every 3 s.]")

    # Collapsible failures section — only when failures exist.
    if failed > 0:
        with st.expander(f"Failed candidates ({failed})", expanded=False):
            for j in jobs:
                if j.get("stage") == "failed":
                    cid = j.get("candidate_id", "")[:16]
                    err = j.get("error") or "Unknown error"
                    st.markdown(f"- `{cid}…` — {err}")
