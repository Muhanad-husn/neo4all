"""
ui/pages/curation_pipeline.py — Layer 2/3 Curation Pipeline UI (SPEC-06/07).

Evidence panel, proposal form, approval queue, and execution panels
extracted from curation.py per SKILL-B R-B7 (>400 line split).

Layer 2 (SPEC-06): Full governed mutation pipeline for manual curation.
  propose -> (approve [-> confirm for high-risk]) -> execute -> audit.
  Evidence retrieval from Qdrant to support human decisions.

Layer 3 (SPEC-07): AI agent pipeline with per-agent model selection.
  Model configuration -> trigger pipeline -> monitor progress.

Architecture rules:
  - No business logic — pure API calls and display (CLAUDE.md §4.1, SKILL-B).
  - All session state via StateManager.get() — no direct st.session_state
    access anywhere in this file (CLAUDE.md §8).
  - API calls via httpx synchronous client (Streamlit is single-threaded).
  - Panels degrade gracefully when the API is unreachable (SKILL-D R-D12).

Backend endpoints consumed:
  GET  /api/curation/evidence/{candidate_id}?run_id=
  POST /api/curation/propose
  GET  /api/curation/proposals/{run_id}
  POST /api/curation/proposals/{id}/approve
  POST /api/curation/proposals/{id}/confirm
  POST /api/curation/proposals/{id}/reject
  POST /api/curation/proposals/{id}/defer
  POST /api/curation/proposals/{id}/execute
  GET  /api/curation/agents/config
  POST /api/curation/agents/run
  GET  /api/monitoring/agents/{run_id}
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import streamlit as st

from ui.components.monitoring_helpers import format_duration
from ui.pages.curation import (
    _delete,
    _fetch,
    _HIGH_RISK_CLASSES,
    _post,
    _PROPOSAL_CLASSES,
    _SEVERITY_BADGE,
    _STATE_BADGE,
    _TYPE_LABELS,
)
from ui.state import StateManager

# ===========================================================================
# Layer 2 — Evidence, Proposals, Approval Gate, Execution (SPEC-06)
# ===========================================================================


def _render_evidence_panel(candidate_id: str, run_id: str) -> None:
    """Fetch and display the top evidence chunks for a candidate from Qdrant."""
    data = _fetch(
        f"/api/curation/evidence/{candidate_id}",
        params={"run_id": run_id},
    )

    if data is None:
        st.warning("Cannot reach the evidence endpoint — evidence unavailable.")
        return

    if data.get("status") == "error":
        for e in data.get("errors", []):
            code = e.get("code", "")
            if code == "candidate_not_found":
                st.info(
                    "No evidence found for this candidate. "
                    "The candidate cache may have expired — re-generate candidates."
                )
            else:
                st.error(f"[{code}] {e.get('message')}")
        return

    chunks: list[dict[str, Any]] = data.get("chunks", [])
    dedupe_keys: list[str] = data.get("dedupe_keys", [])
    qdrant_status: str = data.get("qdrant_status", "")

    if dedupe_keys:
        st.caption(f"Queried {len(dedupe_keys)} element key(s) from Qdrant.")

    if qdrant_status:
        st.caption(f"Qdrant: {qdrant_status}")

    if not chunks:
        if "collection_not_found" in qdrant_status:
            st.warning(
                "Qdrant collection not found for this run. "
                "Chunks may not have been indexed — try re-ingesting the document."
            )
        elif qdrant_status.startswith("error:"):
            st.error(f"Qdrant error: {qdrant_status}")
        else:
            st.info("No evidence chunks found for this candidate's elements.")
        return

    st.caption(f"{len(chunks)} chunk(s) retrieved — top 5 shown:")

    for i, chunk in enumerate(chunks[:5]):
        score = chunk.get("relevance_score", 0.0)
        page = chunk.get("start_page_locator", str(chunk.get("start_page", "")))
        doc_id = chunk.get("doc_id", "")[:12]
        label = f"Chunk {i + 1} — score {score:.3f}  |  doc {doc_id}…  |  page {page}"

        with st.expander(label, expanded=(i == 0)):
            flags = chunk.get("quality_flags", [])
            if flags:
                st.caption(f"Quality flags: {', '.join(flags)}")
            text = chunk.get("text", "")
            st.text(text[:800] + ("…" if len(text) > 800 else ""))


def _render_proposal_form(
    candidate: dict[str, Any], run_id: str, actor: str
) -> None:
    """Render the proposal creation form for a selected candidate."""
    candidate_id: str = candidate.get("candidate_id", "")
    element_refs: list[str] = candidate.get("involved_element_refs", [])

    # Pre-populate target dedupe keys from the candidate's element refs.
    default_keys = "\n".join(element_refs)

    with st.form(key=f"propose_form_{candidate_id[:16]}"):
        proposal_class = st.selectbox(
            "Action class",
            _PROPOSAL_CLASSES,
            help="merge and delete require two-phase approval.",
        )
        rationale = st.text_area(
            "Rationale (required)",
            placeholder="Explain why this curation action is warranted…",
        )
        target_keys_raw = st.text_area(
            "Target dedupe keys (one per line)",
            value=default_keys,
            help="Paste the dedupe_key for each graph element this proposal will act on.",
        )
        element_type = st.radio(
            "Target element type",
            ["node", "relationship"],
            horizontal=True,
        )
        rule_ids_raw = st.text_input(
            "Rule IDs (comma-separated, optional)",
            placeholder="e.g. SCHEMA-001, SCHEMA-004",
        )
        confidence = st.slider(
            "Confidence score",
            min_value=0.0,
            max_value=1.0,
            value=1.0,
            step=0.05,
            help="Human proposals default to 1.0.",
        )

        submitted = st.form_submit_button("Submit Proposal", type="primary")

    if submitted:
        if not rationale.strip():
            st.error("Rationale is required and must be non-empty.")
            return

        target_keys = [k.strip() for k in target_keys_raw.splitlines() if k.strip()]
        targets = [
            {"element_type": element_type, "dedupe_key": k, "label": "", "properties": {}}
            for k in target_keys
        ]
        rule_ids = [r.strip() for r in rule_ids_raw.split(",") if r.strip()]

        payload: dict[str, Any] = {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "proposal_class": proposal_class,
            "targets": targets,
            "rule_ids": rule_ids,
            "rationale": rationale.strip(),
            "confidence_score": confidence,
        }

        with st.spinner("Submitting proposal…"):
            data, err = _post("/api/curation/propose", payload)

        if err or data is None:
            st.error(f"Proposal submission failed: {err or 'No response from API.'}")
            return
        if data.get("status") == "error":
            for e in data.get("errors", []):
                st.error(f"[{e.get('code')}] {e.get('message')}")
            return

        pid = data.get("proposal_id", "")
        st.success(
            f"Proposal created: `{pid[:16]}…`  |  State: {data.get('state', 'pending')}"
        )
        StateManager.get().mark_candidate_submitted(candidate_id)
        st.rerun()


def _render_pending_actions(
    proposal: dict[str, Any], run_id: str, actor: str
) -> None:
    """Render approve / reject / defer buttons for a pending proposal."""
    pid = proposal["proposal_id"]
    pclass = proposal["proposal_class"]
    is_high_risk = pclass in _HIGH_RISK_CLASSES

    sm = StateManager.get()
    pending_token = sm.get_pending_confirmation(pid)

    # If a confirmation token is pending, show the confirm/cancel UI instead.
    if is_high_risk and pending_token:
        st.warning("High-risk action — click Confirm to proceed.")
        col_confirm, col_cancel = st.columns(2)
        with col_confirm:
            if st.button("Confirm Approval", key=f"btn_confirm_{pid}", type="primary"):
                data, err = _post(
                    f"/api/curation/proposals/{pid}/confirm",
                    {
                        "run_id": run_id,
                        "actor": actor,
                        "confirmation_token": pending_token,
                    },
                )
                if err or data is None:
                    st.error(f"Confirmation failed: {err or 'No response from API.'}")
                elif data.get("status") == "error":
                    for e in data.get("errors", []):
                        st.error(f"[{e.get('code')}] {e.get('message')}")
                else:
                    sm.clear_pending_confirmation(pid)
                    approval_id = data.get("approval_id", "")
                    st.success(f"Approved! approval_id: `{approval_id[:16]}…`")
                    st.rerun()
        with col_cancel:
            if st.button("Cancel", key=f"btn_cancel_confirm_{pid}"):
                sm.clear_pending_confirmation(pid)
                st.rerun()
        return

    col_approve, col_reject, col_defer = st.columns(3)

    with col_approve:
        if st.button("Approve", key=f"btn_approve_{pid}", type="primary"):
            data, err = _post(
                f"/api/curation/proposals/{pid}/approve",
                {"run_id": run_id, "actor": actor},
            )
            if err or data is None:
                st.error(f"Approve failed: {err or 'No response from API.'}")
            elif data.get("status") == "error":
                for e in data.get("errors", []):
                    st.error(f"[{e.get('code')}] {e.get('message')}")
            elif data.get("confirmation_required"):
                token = data.get("confirmation_token", "")
                sm.set_pending_confirmation(pid, token)
                st.rerun()
            else:
                approval_id = data.get("approval_id", "")
                st.success(f"Approved! approval_id: `{approval_id[:16]}…`")
                st.rerun()

    with col_reject:
        if st.button("Reject", key=f"btn_reject_{pid}"):
            data, err = _post(
                f"/api/curation/proposals/{pid}/reject",
                {"run_id": run_id, "actor": actor, "reason": ""},
            )
            if err or data is None:
                st.error(f"Reject failed: {err or 'No response from API.'}")
            else:
                st.success("Proposal rejected.")
                st.rerun()

    with col_defer:
        if st.button("Defer", key=f"btn_defer_{pid}"):
            data, err = _post(
                f"/api/curation/proposals/{pid}/defer",
                {"run_id": run_id, "actor": actor, "reason": ""},
            )
            if err or data is None:
                st.error(f"Defer failed: {err or 'No response from API.'}")
            else:
                st.success("Proposal deferred.")
                st.rerun()


def _execute_proposal(
    pid: str, run_id: str, actor: str
) -> dict[str, Any] | None:
    """Approve (idempotent) + execute a single proposal.  Returns execute data or None.

    Both API calls still happen — governance is preserved.  The approve call
    retrieves the deterministic approval_id, and the execute call applies the
    diff via Agent-C.
    """
    # Step 1: Retrieve approval_id (idempotent — already-approved returns the same ID).
    approve_data, approve_err = _post(
        f"/api/curation/proposals/{pid}/approve",
        {"run_id": run_id, "actor": actor},
    )
    if approve_err or approve_data is None:
        st.error(f"Could not retrieve approval ID: {approve_err or 'No response from API.'}")
        return None
    if approve_data.get("status") == "error":
        for e in approve_data.get("errors", []):
            st.error(f"[{e.get('code')}] {e.get('message')}")
        return None
    if approve_data.get("confirmation_required"):
        st.warning(
            "High-risk action — Phase 2 confirmation required before execution. "
            "Complete the confirm step first."
        )
        return None

    approval_id = approve_data.get("approval_id", "")
    if not approval_id:
        st.error("Approve succeeded but returned no approval_id.")
        return None

    # Step 2: Execute with the approval_id.
    exec_data, exec_err = _post(
        f"/api/curation/proposals/{pid}/execute",
        {"run_id": run_id, "approval_id": approval_id, "actor": actor},
    )
    if exec_err or exec_data is None:
        st.error(f"Execution failed: {exec_err or 'No response from API.'}")
        return None
    return exec_data


def _render_execute_panel(
    proposal: dict[str, Any], run_id: str, actor: str
) -> None:
    """Render the one-click execute panel for an approved proposal.

    Clicking Execute internally calls POST /approve (idempotent) to retrieve
    the deterministic approval_id, then immediately calls POST /execute.
    Both API calls happen — governance is fully preserved.
    """
    pid = proposal["proposal_id"]

    st.markdown("**Execute Approved Proposal**")
    st.caption(
        "Click Execute to apply this proposal.  The approval token is "
        "retrieved automatically (idempotent) before execution."
    )

    if st.button("Execute", key=f"btn_execute_{pid}", type="primary"):
        with st.spinner("Executing…"):
            data = _execute_proposal(pid, run_id, actor)

        if data is None:
            return

        if data.get("status") == "error":
            for e in data.get("errors", []):
                st.error(f"[{e.get('code')}] {e.get('message')}")
            return

        outcome = data.get("outcome", "")
        steps = data.get("steps_applied", 0)
        diff_id = data.get("diff_id", "")
        if outcome == "applied":
            st.success(
                f"Executed — {steps} step(s) applied.  "
                f"diff_id: `{diff_id[:16]}…`"
            )
        elif outcome == "failed":
            st.error(
                f"Execution failed — {steps} step(s) applied before failure.  "
                f"diff_id: `{diff_id[:16]}…`"
            )
            err_detail = data.get("error_detail", {})
            if err_detail:
                st.json(err_detail)
        else:
            st.warning(
                f"Outcome: {outcome} — check approval_id and proposal state."
            )
        st.rerun()


def _render_proposal_expander(
    proposal: dict[str, Any], run_id: str, actor: str
) -> None:
    """Render a single proposal in a collapsible expander.

    Shows:
    - Header with ID, class, and state badge.
    - Proposal intent preview (class, rationale, targets).
    - Action area depending on state.
    """
    pid = proposal["proposal_id"]
    pclass = proposal["proposal_class"]
    state = proposal["state"]

    state_badge = _STATE_BADGE.get(state, state.upper())
    is_high_risk = pclass in _HIGH_RISK_CLASSES
    risk_tag = "  :red[HIGH RISK]" if is_high_risk else ""
    label = f"`{pid[:12]}…` — {pclass.upper()}{risk_tag} — {state_badge}"

    with st.expander(label, expanded=(state == "pending")):
        # --- Proposal intent preview ---
        col_a, col_b, col_c = st.columns(3)
        col_a.write(f"**Class:** {pclass}")
        col_b.write(f"**State:** {state}")
        col_c.write(f"**Confidence:** {proposal.get('confidence_score', 1.0):.2f}")

        st.markdown(f"**Rationale:** {proposal.get('rationale', '')}")

        targets = proposal.get("targets", [])
        if targets:
            st.markdown("**Proposal Intent Preview (Targets):**")
            preview_rows = [
                {
                    "Element type": t.get("element_type", ""),
                    "Dedupe key": t.get("dedupe_key", ""),
                    "Label": t.get("label", ""),
                }
                for t in targets
            ]
            st.dataframe(preview_rows, width="stretch", hide_index=True)

        rule_ids = proposal.get("rule_ids", [])
        if rule_ids:
            st.caption(f"Rule IDs: {', '.join(rule_ids)}")

        cand_id = proposal.get("candidate_id", "")
        if cand_id:
            st.caption(f"Candidate: `{cand_id[:16]}…`")

        st.markdown("---")

        # --- Action area ---
        if state == "pending":
            _render_pending_actions(proposal, run_id, actor)
        elif state == "approved":
            _render_execute_panel(proposal, run_id, actor)
        elif state == "executed":
            st.success("This proposal has been executed — diff applied to the graph.")
            if st.button("Dismiss", key=f"btn_dismiss_{pid}"):
                StateManager.get().dismiss_proposal(pid)
                st.rerun()
        else:
            st.caption(f"No actions available — proposal is {state}.")


def _do_batch_approve(
    proposals: list[dict[str, Any]], run_id: str, actor: str,
    include_high_risk: bool = False,
) -> None:
    """Approve all pending proposals.  High-risk are skipped unless include_high_risk."""
    pending = [p for p in proposals if p["state"] == "pending"]
    if include_high_risk:
        batch = pending
    else:
        batch = [p for p in pending if p["proposal_class"] not in _HIGH_RISK_CLASSES]
        high_risk_count = len(pending) - len(batch)
        if high_risk_count > 0:
            st.warning(
                f"Skipping {high_risk_count} high-risk proposal(s) (merge/delete) — "
                "check 'Include high-risk' to include them."
            )

    if not batch:
        st.info("No pending proposals to approve.")
        return

    progress = st.progress(0.0, text="Approving proposals…")
    approved_count = 0
    failed_ids: list[str] = []

    for i, p in enumerate(batch):
        pid = p["proposal_id"]
        data, err = _post(
            f"/api/curation/proposals/{pid}/approve",
            {"run_id": run_id, "actor": actor},
        )
        if err or data is None or data.get("status") == "error":
            failed_ids.append(pid[:12])
        elif data.get("confirmation_required"):
            # High-risk: auto-confirm with the returned token.
            token = data.get("confirmation_token", "")
            confirm_data, confirm_err = _post(
                f"/api/curation/proposals/{pid}/confirm",
                {"run_id": run_id, "actor": actor, "confirmation_token": token},
            )
            if confirm_err or confirm_data is None or confirm_data.get("status") == "error":
                failed_ids.append(pid[:12])
            else:
                approved_count += 1
        else:
            approved_count += 1
        progress.progress((i + 1) / len(batch), text=f"Approved {approved_count}/{len(batch)}")

    progress.empty()

    if approved_count > 0:
        st.success(f"Approved {approved_count} proposal(s).")
    if failed_ids:
        st.error(f"Failed to approve {len(failed_ids)} proposal(s): {', '.join(failed_ids)}…")

    st.rerun()


def _do_batch_execute(
    proposals: list[dict[str, Any]], run_id: str, actor: str
) -> None:
    """Execute all approved proposals (one-click: approve-idempotent + execute)."""
    approved = [p for p in proposals if p["state"] == "approved"]

    if not approved:
        st.info("No approved proposals to execute.")
        return

    progress = st.progress(0.0, text="Executing proposals…")
    applied = 0
    failed = 0
    skipped = 0

    for i, p in enumerate(approved):
        pid = p["proposal_id"]
        data = _execute_proposal(pid, run_id, actor)

        if data is None:
            failed += 1
        elif data.get("status") == "error":
            failed += 1
        elif data.get("outcome") == "applied":
            applied += 1
        elif data.get("outcome") == "failed":
            failed += 1
        else:
            skipped += 1

        progress.progress(
            (i + 1) / len(approved),
            text=f"Executed {i + 1}/{len(approved)} — {applied} applied, {failed} failed",
        )

    progress.empty()

    parts: list[str] = []
    if applied:
        parts.append(f"{applied} applied")
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    summary = ", ".join(parts) or "no proposals processed"

    if failed == 0 and applied > 0:
        st.success(f"Batch execution complete: {summary}.")
    elif applied > 0:
        st.warning(f"Batch execution complete (partial): {summary}.")
    else:
        st.error(f"Batch execution failed: {summary}.")

    st.rerun()


def _render_batch_actions(
    proposals: list[dict[str, Any]], run_id: str, actor: str
) -> None:
    """Render batch approve/execute/dismiss buttons above the proposal list."""
    pending = [p for p in proposals if p["state"] == "pending"]
    approved = [p for p in proposals if p["state"] == "approved"]
    executed = [p for p in proposals if p["state"] == "executed"]

    low_risk_pending = [p for p in pending if p["proposal_class"] not in _HIGH_RISK_CLASSES]

    include_high_risk = st.checkbox(
        "Include high-risk (merge/delete)",
        key="batch_include_high_risk",
        help="When checked, batch Approve All includes high-risk proposals.",
    )

    approve_count = len(pending) if include_high_risk else len(low_risk_pending)

    col_approve, col_execute, col_dismiss = st.columns(3)

    with col_approve:
        label = f"Approve All Pending ({approve_count})"
        if st.button(
            label,
            key="batch_approve",
            disabled=(approve_count == 0),
            help=(
                "Approves all pending proposals including high-risk."
                if include_high_risk
                else "Approves all low-risk pending proposals. High-risk (merge/delete) are skipped."
            ),
        ):
            _do_batch_approve(proposals, run_id, actor, include_high_risk=include_high_risk)

    with col_execute:
        label = f"Execute All Approved ({len(approved)})"
        if st.button(
            label,
            key="batch_execute",
            disabled=(len(approved) == 0),
            type="primary",
            help="Executes all approved proposals via Agent-C. Shows progress.",
        ):
            _do_batch_execute(proposals, run_id, actor)

    with col_dismiss:
        label = f"Dismiss All Executed ({len(executed)})"
        if st.button(
            label,
            key="batch_dismiss",
            disabled=(len(executed) == 0),
            help="Hides all executed proposals from the queue. Audit trail is preserved.",
        ):
            sm = StateManager.get()
            for p in executed:
                sm.dismiss_proposal(p["proposal_id"])
            st.rerun()


def _render_proposal_queue(run_id: str, actor: str) -> None:
    """Fetch and display all proposals for a run with per-proposal action panels."""
    st.subheader("Proposal Queue")

    btn_col1, btn_col2 = st.columns([1, 1])
    with btn_col1:
        if st.button("Refresh proposals", key="refresh_proposals"):
            st.rerun()
    with btn_col2:
        if st.button("Clear all proposals", key="clear_all_proposals", type="secondary"):
            resp, err = _delete(f"/api/curation/proposals/{run_id}")
            if err or resp is None:
                st.error(f"Failed to clear proposals: {err or 'No response.'}")
            else:
                StateManager.get().clear_dismissed_proposals()
                st.success("All proposals cleared.")
                st.rerun()

    data = _fetch(f"/api/curation/proposals/{run_id}")

    if data is None:
        st.warning(
            "Cannot reach the proposals endpoint — queue unavailable. "
            "Check that the API server is running."
        )
        return

    if data.get("status") == "error":
        for e in data.get("errors", []):
            st.error(f"[{e.get('code')}] {e.get('message')}")
        return

    proposals: list[dict[str, Any]] = data.get("proposals", [])
    total: int = data.get("total", 0)

    if not proposals:
        st.info(
            "No proposals yet for this run.  "
            "Select a candidate above and submit a proposal."
        )
        return

    # Filter out dismissed proposals.
    sm = StateManager.get()
    dismissed = sm.dismissed_proposals
    hidden_count = sum(1 for p in proposals if p["proposal_id"] in dismissed)
    visible = [p for p in proposals if p["proposal_id"] not in dismissed]

    pending = sum(1 for p in visible if p["state"] == "pending")
    approved = sum(1 for p in visible if p["state"] == "approved")
    executed = sum(1 for p in visible if p["state"] == "executed")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total", total)
    c2.metric("Pending", pending)
    c3.metric("Approved", approved)
    c4.metric("Executed", executed)

    # Batch actions — approve all pending / execute all approved / dismiss executed.
    _render_batch_actions(visible, run_id, actor)

    # Show hidden count and "Show all" button.
    if hidden_count > 0:
        col_hidden, col_show = st.columns([3, 1])
        col_hidden.caption(f"{hidden_count} dismissed proposal(s) hidden.")
        with col_show:
            if st.button("Show all", key="show_all_dismissed"):
                for p in proposals:
                    sm.undismiss_proposal(p["proposal_id"])
                st.rerun()

    st.divider()

    for proposal in visible:
        _render_proposal_expander(proposal, run_id, actor)


def _render_candidate_detail_section(
    groups: list[dict[str, Any]], run_id: str, actor: str
) -> None:
    """Render candidate selector, evidence panel, and proposal form.

    Groups is the list returned by GET /api/curation/candidates/{run_id}.
    """
    st.subheader("Candidate Detail — Evidence & Propose")

    # Collect a flat list of all candidates across all groups.
    all_candidates: list[dict[str, Any]] = [
        c for g in groups for c in g.get("candidates", [])
    ]

    if not all_candidates:
        st.info("Generate candidates first using the button above.")
        return

    # Build selectbox options.
    none_opt = "— Select a candidate —"
    option_labels: dict[str, str] = {
        none_opt: none_opt,
        **{
            c["candidate_id"]: (
                f"{c['candidate_id'][:14]}…  "
                f"[{c['severity'].upper()}]  "
                f"{_TYPE_LABELS.get(c['candidate_type'], c['candidate_type'])}"
            )
            for c in all_candidates
        },
    }
    options = [none_opt] + [c["candidate_id"] for c in all_candidates]

    selected_id = st.selectbox(
        "Select candidate",
        options=options,
        format_func=lambda x: option_labels.get(x, x),
    )

    if selected_id == none_opt:
        st.caption("Select a candidate to view evidence and create a proposal.")
        return

    candidate = next(
        (c for c in all_candidates if c["candidate_id"] == selected_id), None
    )
    if candidate is None:
        st.warning("Selected candidate not found in current cache.")
        return

    # --- Candidate detail ---
    st.markdown("**Candidate Details**")
    col_type, col_sev, col_lane = st.columns(3)
    col_type.write(f"**Type:** {_TYPE_LABELS.get(candidate['candidate_type'], candidate['candidate_type'])}")
    col_sev.write(f"**Severity:** {_SEVERITY_BADGE.get(candidate['severity'], candidate['severity'])}")
    col_lane.write(f"**Lane:** {candidate.get('candidate_lane', '')}")
    st.caption(f"Method: {candidate.get('detection_method', '')}")

    refs = candidate.get("involved_element_refs", [])
    if refs:
        st.markdown("**Involved elements:**")
        for ref in refs:
            st.code(ref, language=None)

    ctx = candidate.get("collision_context", {})
    if ctx:
        st.markdown("**Detection context:**")
        st.json(ctx)

    st.divider()

    # --- Evidence panel ---
    st.markdown("**Evidence from Qdrant**")
    _render_evidence_panel(selected_id, run_id)

    st.divider()

    # --- Proposal form ---
    sm = StateManager.get()
    if selected_id in sm.submitted_candidates:
        st.success("Proposal submitted for this candidate.")
    else:
        st.markdown("**Create Proposal for this Candidate**")
        st.caption(
            "Human proposals follow the same governed pipeline as AI proposals — "
            "no bypass.  All mutations require approval before execution."
        )
        _render_proposal_form(candidate, run_id, actor)


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
        default_a = "openai/gpt-5-mini"
        default_b = "openai/gpt-5-mini"
        default_p = "openai/gpt-5-mini"
    else:
        default_a = config_data.get("agent_a", {}).get("model", "openai/gpt-5-mini")
        default_b = config_data.get("agent_b", {}).get("model", "openai/gpt-5-mini")
        default_p = config_data.get("agent_p", {}).get("model", "openai/gpt-5-mini")

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
        payload: dict[str, Any] = {
            "run_id": run_id,
            "model_a": model_a.strip() or None,
            "model_b": model_b.strip() or None,
            "model_p": model_p.strip() or None,
        }
        if _all_candidate_ids:
            payload["candidate_ids"] = _all_candidate_ids

        with st.spinner("Enqueuing agent pipeline jobs..."):
            data, err = _post("/api/curation/agents/run", payload)

        if err or data is None:
            st.error(f"Pipeline trigger failed: {err or 'No response from API.'}")
        elif data.get("status") == "error":
            for e in data.get("errors", []):
                st.error(f"[{e.get('code')}] {e.get('message')}")
        else:
            enqueued = data.get("jobs_enqueued", 0)
            st.success(
                f"Pipeline started — {enqueued} job(s) enqueued.  "
                f"Models: A={data.get('model_a', '?')}, "
                f"B={data.get('model_b', '?')}, "
                f"P={data.get('model_p', '?')}"
            )

    # Agent pipeline progress — fragment fetches its own data on each rerun.
    _agent_pipeline_fragment(run_id)


@st.fragment(run_every=timedelta(seconds=3))
def _agent_pipeline_fragment(run_id: str) -> None:
    """Fragment wrapper around agent pipeline progress with auto-refresh.

    Always fetches fresh status data on each rerun so the progress display
    stays current.  Reruns every 3 s (fragment-scoped — does not cause a
    full page rebuild).
    """
    _render_agent_pipeline_progress(run_id)


def _render_agent_pipeline_progress(run_id: str) -> None:
    """Fetch and display per-candidate agent pipeline status."""
    st.markdown("**Agent Pipeline Progress**")

    if st.button("Refresh progress", key="refresh_agent_progress"):
        st.rerun()

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

    # Collapsible failures section — only when failures exist.
    if failed > 0:
        with st.expander(f"Failed candidates ({failed})", expanded=False):
            for j in jobs:
                if j.get("stage") == "failed":
                    cid = j.get("candidate_id", "")[:16]
                    err = j.get("error") or "Unknown error"
                    st.markdown(f"- `{cid}…` — {err}")

