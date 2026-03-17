"""
ui/pages/curation.py — Phase 4: Curation Layer 1 (SPEC-05 S-05.5).

Layer 1 (SPEC-05): Deterministic candidate generation and review.
  Detectors: exact-node-dup, exact-rel-dup, probable-dup, canonical-violation,
  structural-anomaly.  Zero-LLM, cached 5 minutes.

Layer 2 (SPEC-06) rendering is delegated to ui/pages/curation_pipeline.py
(evidence panel, proposal form, approval queue, execution panels).

Architecture rules:
  - No business logic — pure API calls and display (CLAUDE.md §4.1, SKILL-B).
  - All session state via StateManager.get() — no direct st.session_state
    access anywhere in this file (CLAUDE.md §8).
  - API calls via httpx synchronous client (Streamlit is single-threaded).
  - Panels degrade gracefully when the API is unreachable (SKILL-D R-D12).

Backend endpoints consumed (Layer 1):
  POST /api/curation/candidates/generate
  GET  /api/curation/candidates/{run_id}

See ui/pages/curation_pipeline.py for Layer 2 endpoints.
"""

import os
from typing import Any

import httpx
import streamlit as st

from api.models.run import Phase
from ui.components.candidate_summary import method_label, summarize_candidate
from ui.state import StateManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8000")
_REQUEST_TIMEOUT: float = 30.0

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

_SEVERITY_BADGE: dict[str, str] = {
    "critical": ":red[CRITICAL]",
    "high": ":orange[HIGH]",
    "medium": ":blue[MEDIUM]",
    "low": ":gray[LOW]",
}

_TYPE_LABELS: dict[str, str] = {
    "exact_node_duplicate": "Exact Node Duplicates",
    "exact_rel_duplicate": "Exact Relationship Duplicates",
    "probable_duplicate": "Probable Duplicates",
    "canonical_violation": "Canonical / Inverse Violations",
    "structural_anomaly": "Structural Anomalies",
}

_STATE_BADGE: dict[str, str] = {
    "pending": ":blue[PENDING]",
    "approved": ":green[APPROVED]",
    "executed": ":violet[EXECUTED]",
    "rejected": ":red[REJECTED]",
    "deferred": ":gray[DEFERRED]",
    "excluded": ":orange[EXCLUDED]",
}

# proposal_class values that require two-phase approval (mirrors backend)
_HIGH_RISK_CLASSES: frozenset[str] = frozenset({"merge", "delete"})

_PROPOSAL_CLASSES: list[str] = [
    "canonicalize",
    "normalize",
    "rename",
    "merge",
    "delete",
    "defer",
]

# ---------------------------------------------------------------------------
# API helpers — errors return None / error tuple, never raise
# ---------------------------------------------------------------------------


def _fetch(
    path: str, params: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """GET {_API_BASE_URL}{path} and return parsed JSON or None on any error."""
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as c:
            r = c.get(f"{_API_BASE_URL}{path}", params=params)
            r.raise_for_status()
            return r.json()  # type: ignore[no-any-return]
    except Exception:
        return None


def _post(
    path: str, payload: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """POST {_API_BASE_URL}{path} with JSON body. Returns (data, error)."""
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as c:
            r = c.post(f"{_API_BASE_URL}{path}", json=payload)
            return r.json(), None  # type: ignore[no-any-return]
    except Exception as exc:
        return None, str(exc)


def _delete(path: str) -> tuple[dict[str, Any] | None, str | None]:
    """DELETE {_API_BASE_URL}{path}. Returns (data, error)."""
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as c:
            r = c.delete(f"{_API_BASE_URL}{path}")
            return r.json(), None  # type: ignore[no-any-return]
    except Exception as exc:
        return None, str(exc)


# ===========================================================================
# Layer 1 — Candidate Review (SPEC-05)
# ===========================================================================


def _dismiss_stale_proposals(run_id: str) -> None:
    """Dismiss all existing proposals for the run after candidate regeneration.

    Proposals are preserved in S3 for audit; this only hides them from the UI
    queue so stale proposals from a previous candidate set are not shown.
    """
    data = _fetch(f"/api/curation/proposals/{run_id}")
    if data is None or data.get("status") == "error":
        return
    proposals: list[dict[str, Any]] = data.get("proposals", [])
    if not proposals:
        return
    ids = {p["proposal_id"] for p in proposals}
    StateManager.get().dismiss_proposals_batch(ids)


def _run_stage_detection(
    run_id: str, stage: int | None, label: str
) -> None:
    """Run candidate detection for a specific stage and display results."""
    with st.spinner(f"Running {label} detection…"):
        payload: dict[str, Any] = {"run_id": run_id}
        if stage is not None:
            payload["stage"] = stage
        data, err = _post(
            "/api/curation/candidates/generate",
            payload,
        )

    if err or data is None:
        st.error(
            f"Candidate generation failed: {err or 'No response from API.'}"
        )
        return

    if data.get("status") == "error":
        for e in data.get("errors", []):
            code = e.get("code", "")
            if code == "schema_not_locked":
                st.error(
                    "Domain schema is not locked. Complete Phase 1 "
                    "(Domain Schema) before generating candidates."
                )
            else:
                st.error(f"[{code}] {e.get('message')}")
        return

    total: int = data.get("total_count", 0)
    counts: list[dict[str, Any]] = data.get("counts_by_type", [])
    sv: str = data.get("schema_version", "")

    if total == 0:
        st.success(f"{label} detection complete — no candidates found.")
    else:
        st.success(f"{label} detection complete — {total} candidate(s) found.")

    if counts:
        st.markdown("**Candidates by detector type:**")
        cols = st.columns(max(len(counts), 1))
        for col, entry in zip(cols, counts):
            type_label = _TYPE_LABELS.get(
                entry["candidate_type"], entry["candidate_type"]
            )
            col.metric(type_label, entry["count"])

    if sv:
        st.caption(f"Schema version: `{sv[:16]}…`")

    # Dismiss stale proposals after regeneration.
    _dismiss_stale_proposals(run_id)
    st.rerun()


def _clear_candidates(run_id: str) -> None:
    """Clear all cached candidates, proposals, and graph query cache for a run."""
    data, err = _delete(f"/api/curation/candidates/{run_id}")
    if err or data is None:
        st.error(f"Clear failed: {err or 'No response from API.'}")
        return
    if data.get("status") == "error":
        for e in data.get("errors", []):
            st.error(f"[{e.get('code')}] {e.get('message')}")
        return

    cand = data.get("candidates_cleared", 0)
    props = data.get("proposals_cleared", 0)
    st.success(
        f"Cleared {cand} candidate cache entries and {props} proposals. "
        "You can now regenerate candidates."
    )
    StateManager.get().clear_dismissed_proposals()
    st.rerun()


def _render_trigger_section(run_id: str) -> None:
    """Render staged candidate generation tabs with action buttons on the right."""
    # Header row: title on the left, action buttons on the right.
    col_title, col_actions = st.columns([3, 2])
    with col_title:
        st.subheader("Generate Candidates")
        st.caption(
            "Staged detection pipeline: resolve duplicates first, then canonical "
            "violations, then structural issues. Each stage runs against the "
            "current graph state. Zero-LLM — results are fully reproducible."
        )
    with col_actions:
        st.write("")  # spacing
        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            gen_all = st.button(
                "Generate All Stages",
                type="primary",
                key="gen_all_stages",
                help="Run all three detection stages sequentially.",
            )
        with btn_col2:
            clear_all = st.button(
                "Clear All Candidates",
                key="btn_clear_candidates",
                help="Remove all cached candidates, proposals, and graph query cache. "
                "Use this to start fresh before regenerating.",
            )

    if gen_all:
        _run_stage_detection(run_id, stage=None, label="All stages")
    if clear_all:
        _clear_candidates(run_id)

    tab_dup, tab_canon, tab_struct = st.tabs([
        "Stage 1: Duplicates",
        "Stage 2: Canonical Violations",
        "Stage 3: Structural Issues",
    ])

    with tab_dup:
        st.caption(
            "Runs exact node/rel duplicate and probable duplicate detectors. "
            "Duplicate chains are automatically detected and annotated for "
            "canonicalization."
        )
        if st.button(
            "Detect Duplicates",
            type="primary",
            key="gen_stage_1",
            help="Runs ExactNodeDuplicate, ExactRelDuplicate, and ProbableDuplicate detectors.",
        ):
            _run_stage_detection(run_id, stage=1, label="Duplicate")

    with tab_canon:
        st.caption(
            "Runs canonical violation detector (direction & inverse violations). "
            "Best run after duplicate resolution so the graph is cleaner."
        )
        if st.button(
            "Detect Canonical Violations",
            type="primary",
            key="gen_stage_2",
            help="Runs CanonicalViolationDetector against the current graph.",
        ):
            _run_stage_detection(run_id, stage=2, label="Canonical violation")

    with tab_struct:
        st.caption(
            "Runs structural anomaly detectors (orphans, missing provenance, "
            "qualifier gaps). Best run after canonical violations are resolved."
        )
        if st.button(
            "Detect Structural Issues",
            type="primary",
            key="gen_stage_3",
            help="Runs StructuralAnomalyDetector (degree_outlier excluded).",
        ):
            _run_stage_detection(run_id, stage=3, label="Structural issue")


def _render_severity_badges(severity_counts: dict[str, int]) -> None:
    """Render a horizontal severity badge row for a candidate group."""
    parts: list[str] = []
    for sev in ("critical", "high", "medium", "low"):
        n = severity_counts.get(sev, 0)
        if n > 0:
            badge = _SEVERITY_BADGE.get(sev, sev.upper())
            parts.append(f"{badge} ×{n}")
    if parts:
        st.caption("  |  ".join(parts))


def _format_refs(refs: list[str]) -> str:
    """Join involved_element_refs with truncation for compact display."""
    if not refs:
        return ""
    truncated = [r[:24] for r in refs[:3]]
    joined = ", ".join(truncated)
    if len(refs) > 3:
        joined += f" +{len(refs) - 3} more"
    return joined


def _format_context(ctx: dict[str, Any]) -> str:
    """Render key context fields as a compact diagnostic string."""
    skip = {"dedupe_key", "rel_dedupe_key"}
    parts = [f"{k}: {v}" for k, v in ctx.items() if k not in skip]
    return "  |  ".join(str(p) for p in parts[:4])


def _render_candidate_group(
    group: dict[str, Any],
    run_id: str = "",
    actor: str = "",
) -> None:
    """Render one candidate type group inside a collapsible expander."""
    ctype: str = group.get("candidate_type", "")
    total: int = group.get("total", 0)
    severity_counts: dict[str, int] = group.get("severity_counts", {})
    candidates: list[dict[str, Any]] = group.get("candidates", [])

    label = _TYPE_LABELS.get(ctype, ctype.replace("_", " ").title())
    has_critical = severity_counts.get("critical", 0) > 0

    with st.expander(f"{label} — {total} found", expanded=has_critical):
        _render_severity_badges(severity_counts)

        if not candidates:
            st.caption("No candidates in this group.")
            return

        rows = [
            {
                "ID": c.get("candidate_id", "")[:12] + "…",
                "Severity": c.get("severity", "").upper(),
                "Summary": summarize_candidate(c),
                "Method": method_label(c.get("detection_method", "")),
            }
            for c in candidates
        ]
        st.dataframe(rows, width="stretch", hide_index=True)

        # Bulk orphan delete button for structural anomaly groups.
        if ctype == "structural_anomaly" and run_id and actor:
            orphans = [
                c for c in candidates
                if c.get("detection_method") == "orphan_node"
            ]
            if orphans:
                if st.button(
                    f"Delete All Orphan Nodes ({len(orphans)})",
                    key="btn_delete_all_orphans",
                    type="secondary",
                    help="Creates, approves, and executes delete proposals for all orphan nodes.",
                ):
                    with st.spinner("Deleting all orphan nodes…"):
                        data, err = _post(
                            "/api/curation/orphans/delete-all",
                            {"run_id": run_id, "actor": actor},
                        )
                    if err or data is None:
                        st.error(f"Bulk delete failed: {err or 'No response from API.'}")
                    elif data.get("status") == "error":
                        for e in data.get("errors", []):
                            st.error(f"[{e.get('code')}] {e.get('message')}")
                    else:
                        executed = data.get("proposals_executed", 0)
                        failed = data.get("failed", 0)
                        st.success(
                            f"Orphan deletion complete — {executed} deleted, "
                            f"{failed} failed."
                        )
                        st.rerun()


def _render_candidates_section(run_id: str) -> None:
    """Fetch candidates from cache and render grouped by detector type."""
    st.subheader("Candidate Review")

    data = _fetch(f"/api/curation/candidates/{run_id}")

    if data is None:
        st.warning(
            "Cannot reach the candidates endpoint — list unavailable. "
            "Check that the API server is running."
        )
        return

    if data.get("status") == "error":
        for e in data.get("errors", []):
            st.error(f"[{e.get('code')}] {e.get('message')}")
        return

    groups: list[dict[str, Any]] = data.get("groups", [])
    total: int = data.get("total_count", 0)
    sv: str = data.get("schema_version", "")

    if not groups:
        st.info(
            "No candidates loaded yet.  Click **Generate Candidates** above "
            "to run detection, or wait if a previous run is in progress."
        )
        return

    col_total, col_types = st.columns([1, 3])
    col_total.metric("Total Candidates", total)
    col_types.metric("Detector Types with Findings", len(groups))
    if sv:
        st.caption(f"Schema version: `{sv[:16]}…`")

    st.divider()

    for group in groups:
        _render_candidate_group(group)


# ===========================================================================
# Main
# ===========================================================================


def main() -> None:
    """Curation page entry point, called by Streamlit on every script rerun."""
    # Lazy import to avoid circular dependency — curation_pipeline imports
    # shared helpers (_fetch, _post, constants) from this module.
    from ui.pages.curation_agents import (  # noqa: E402
        _render_agent_model_config,
    )
    from ui.pages.curation_pipeline import (  # noqa: E402
        _render_candidate_detail_section,
        _render_excluded_items,
        _render_proposal_queue,
    )

    # NOTE: st.set_page_config() is called by app.py before routing here.
    # Calling it again raises StreamlitAPIException in Streamlit 1.40+.

    state = StateManager.get()

    if state.phase.value < Phase.CURATION.value:
        st.title("Phase 4: Curation")
        st.warning(
            "Phase 3 (AI-Assisted Extraction) must be completed before running "
            "curation.  Complete extraction and click 'Proceed to Curation' on "
            "the Extraction page."
        )
        return

    run_id = state.run_id
    if not run_id:
        st.error("No active session.  Initialize a session in Phase 0.")
        return

    # Default actor identity from session (neo4j_user).
    actor: str = state.neo4j_user or "operator"

    with st.sidebar:
        st.header("Curation")
        st.caption("Run ID")
        st.code(run_id[:16] + "…", language=None)
        if state.schema_version:
            st.caption("Schema version")
            st.write(state.schema_version[:16] + "…")
        st.caption(f"Actor: `{actor}`")
        st.divider()
        st.caption(f"API: `{_API_BASE_URL}`")
        st.divider()
        st.button(
            "Add More Documents",
            on_click=lambda: StateManager.get().reenter_phase(Phase.INGESTION),
            help="Return to ingestion to add more documents. Existing work is preserved.",
        )
        st.button(
            "Back to Extraction",
            on_click=lambda: StateManager.get().reenter_phase(Phase.EXTRACTION),
            help="Return to extraction to review and re-run failed chunks.",
        )

    # Scroll to top on page load.
    st.markdown(
        '<div id="top"></div>'
        "<script>window.parent.document.querySelector('section.main').scrollTo(0, 0);</script>",
        unsafe_allow_html=True,
    )

    st.title("Phase 4: Curation")
    st.caption(
        "Layer 1: Zero-LLM candidate detection.  "
        "Layer 2: Evidence review, governed proposal pipeline, and execution.  "
        "Layer 3: AI agent pipeline with per-agent model selection."
    )

    # --- Layer 1: Candidate generation trigger ---
    _render_trigger_section(run_id)

    st.divider()

    # --- Layer 1: Candidate review (reads from 5-minute cache) ---
    # Also captures the groups list for use in Layer 2 selector.
    data = _fetch(f"/api/curation/candidates/{run_id}")
    groups: list[dict[str, Any]] = []

    if data is None:
        st.warning(
            "Cannot reach the candidates endpoint — list unavailable. "
            "Check that the API server is running."
        )
    elif data.get("status") == "error":
        for e in data.get("errors", []):
            st.error(f"[{e.get('code')}] {e.get('message')}")
    else:
        groups = data.get("groups", [])
        total: int = data.get("total_count", 0)
        sv: str = data.get("schema_version", "")

        if not groups:
            st.info(
                "No candidates loaded yet.  Click **Generate Candidates** above "
                "to run detection, or wait if a previous run is in progress."
            )
        else:
            col_total, col_types = st.columns([1, 3])
            col_total.metric("Total Candidates", total)
            col_types.metric("Detector Types with Findings", len(groups))
            if sv:
                st.caption(f"Schema version: `{sv[:16]}…`")
            st.divider()
            for group in groups:
                _render_candidate_group(group, run_id=run_id, actor=actor)

    # --- Layer 2: Candidate detail, evidence, proposal form ---
    st.divider()
    _render_candidate_detail_section(groups, run_id, actor)

    st.divider()

    # --- Layer 2: Proposal queue ---
    _render_proposal_queue(run_id, actor)

    st.divider()

    # --- Layer 2: Excluded items ---
    _render_excluded_items(run_id, actor)

    st.divider()

    # --- Layer 3: AI Agent Pipeline (SPEC-07) ---
    _render_agent_model_config(run_id)


if __name__ == "__main__":
    main()
