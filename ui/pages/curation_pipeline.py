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

from typing import Any

import streamlit as st

from ui.pages.curation import (
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
        st.rerun()


def _render_pending_actions(
    proposal: dict[str, Any], run_id: str, actor: str
) -> None:
    """Render approve / reject / defer buttons for a pending proposal."""
    pid = proposal["proposal_id"]
    pclass = proposal["proposal_class"]
    is_high_risk = pclass in _HIGH_RISK_CLASSES

    approve_label = "Approve (Phase 1)" if is_high_risk else "Approve"

    col_approve, col_reject, col_defer = st.columns(3)

    with col_approve:
        if st.button(approve_label, key=f"btn_approve_{pid}", type="primary"):
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
                st.warning("High-risk action — Phase 2 confirmation required.")
                st.info("Copy this token and paste it into the Confirm form below:")
                st.code(token, language=None)
            else:
                approval_id = data.get("approval_id", "")
                st.success("Approved!")
                st.info("Copy this approval ID — you will need it to execute:")
                st.code(approval_id, language=None)
                # No st.rerun() here — keep the approval_id visible so the user
                # can copy it before the page refreshes.

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

    # Two-phase confirmation form — always shown for high-risk pending proposals.
    if is_high_risk:
        st.markdown("---")
        st.markdown("**Phase 2 — Confirm High-Risk Approval**")
        st.caption(
            "After clicking 'Approve (Phase 1)' above, paste the confirmation "
            "token into the field below to complete the two-phase approval."
        )
        with st.form(key=f"confirm_form_{pid}"):
            token_input = st.text_input(
                "Confirmation token",
                placeholder="Paste the token shown after Phase 1 approve",
            )
            actor_input = st.text_input("Actor", value=actor)
            submitted = st.form_submit_button("Confirm Approval", type="primary")

        if submitted:
            if not token_input.strip():
                st.error("Confirmation token is required.")
            else:
                data, err = _post(
                    f"/api/curation/proposals/{pid}/confirm",
                    {
                        "run_id": run_id,
                        "actor": actor_input or actor,
                        "confirmation_token": token_input.strip(),
                    },
                )
                if err or data is None:
                    st.error(f"Confirmation failed: {err or 'No response from API.'}")
                elif data.get("status") == "error":
                    for e in data.get("errors", []):
                        st.error(f"[{e.get('code')}] {e.get('message')}")
                else:
                    approval_id = data.get("approval_id", "")
                    st.success("Approval confirmed!")
                    st.info("Copy this approval ID — you will need it to execute:")
                    st.code(approval_id, language=None)
                    # No st.rerun() — keep approval_id visible.


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
            st.dataframe(preview_rows, use_container_width=True, hide_index=True)

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
    proposals: list[dict[str, Any]], run_id: str, actor: str
) -> None:
    """Approve all low-risk pending proposals.  High-risk (merge/delete) are skipped."""
    pending = [p for p in proposals if p["state"] == "pending"]
    low_risk = [p for p in pending if p["proposal_class"] not in _HIGH_RISK_CLASSES]
    high_risk_count = len(pending) - len(low_risk)

    if not low_risk:
        st.info("No low-risk pending proposals to approve.")
        return

    if high_risk_count > 0:
        st.warning(
            f"Skipping {high_risk_count} high-risk proposal(s) (merge/delete) — "
            "these require manual two-phase confirmation."
        )

    progress = st.progress(0.0, text="Approving proposals…")
    approved_count = 0
    failed_ids: list[str] = []

    for i, p in enumerate(low_risk):
        pid = p["proposal_id"]
        data, err = _post(
            f"/api/curation/proposals/{pid}/approve",
            {"run_id": run_id, "actor": actor},
        )
        if err or data is None or data.get("status") == "error":
            failed_ids.append(pid[:12])
        else:
            approved_count += 1
        progress.progress((i + 1) / len(low_risk), text=f"Approved {approved_count}/{len(low_risk)}")

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

    col_approve, col_execute, col_dismiss = st.columns(3)

    with col_approve:
        label = f"Approve All Pending ({len(low_risk_pending)})"
        if st.button(
            label,
            key="batch_approve",
            disabled=(len(low_risk_pending) == 0),
            help="Approves all low-risk pending proposals. High-risk (merge/delete) are skipped.",
        ):
            _do_batch_approve(proposals, run_id, actor)

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

    if st.button("Refresh proposals", key="refresh_proposals"):
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
        default_a = "openai/gpt-4o-mini"
        default_b = "openai/gpt-4o-mini"
        default_p = "openai/gpt-4o-mini"
    else:
        default_a = config_data.get("agent_a", {}).get("model", "openai/gpt-4o-mini")
        default_b = config_data.get("agent_b", {}).get("model", "openai/gpt-4o-mini")
        default_p = config_data.get("agent_p", {}).get("model", "openai/gpt-4o-mini")

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

    # Trigger button.
    if st.button(
        "Run AI Agent Pipeline",
        type="primary",
        key="run_agent_pipeline",
        help=(
            "Enqueues all detected candidates for processing by the AI agent chain. "
            "Progress is shown below and in the Monitoring page."
        ),
    ):
        payload: dict[str, Any] = {
            "run_id": run_id,
            "model_a": model_a.strip() or None,
            "model_b": model_b.strip() or None,
            "model_p": model_p.strip() or None,
        }

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

    # Agent pipeline progress.
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
    running = total - completed - failed - deferred

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total", total)
    c2.metric("Running", running)
    c3.metric("Complete", completed)
    c4.metric("Failed", failed)

    # Stage badge mapping.
    stage_badge: dict[str, str] = {
        "queued": ":gray[QUEUED]",
        "evidence_running": ":blue[EVIDENCE]",
        "evidence_complete": ":blue[EVIDENCE OK]",
        "retrieval_running": ":orange[RETRIEVAL]",
        "retrieval_complete": ":orange[RETRIEVAL OK]",
        "proposal_running": ":violet[PROPOSAL]",
        "complete": ":green[COMPLETE]",
        "failed": ":red[FAILED]",
        "deferred": ":gray[DEFERRED]",
    }

    # Job list.
    rows = [
        {
            "Candidate": j.get("candidate_id", "")[:14] + "...",
            "Stage": stage_badge.get(j.get("stage", ""), j.get("stage", "")),
            "Evidence": j.get("evidence_items", 0),
            "Sufficient": "Yes" if j.get("evidence_sufficient") else "No",
            "Retrieval Rounds": j.get("retrieval_rounds", 0),
            "Proposal": (j.get("proposal_id") or "")[:14] + ("..." if j.get("proposal_id") else "-"),
            "Error": j.get("error") or "-",
        }
        for j in jobs
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)
