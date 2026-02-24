"""
ui/pages/curation.py — Phase 4: Curation Layers 1 & 2 (SPEC-05 S-05.5, SPEC-06 S-06.9).

Layer 1 (SPEC-05): Deterministic candidate generation and review.
  Detectors: exact-node-dup, exact-rel-dup, probable-dup, canonical-violation,
  structural-anomaly.  Zero-LLM, cached 5 minutes.

Layer 2 (SPEC-06): Full governed mutation pipeline for manual curation.
  propose → (approve [→ confirm for high-risk]) → execute → audit.
  Evidence retrieval from Qdrant to support human decisions.

Architecture rules:
  - No business logic — pure API calls and display (CLAUDE.md §4.1, SKILL-B).
  - All session state via StateManager.get() — no direct st.session_state
    access anywhere in this file (CLAUDE.md §8).
  - API calls via httpx synchronous client (Streamlit is single-threaded).
  - Panels degrade gracefully when the API is unreachable (SKILL-D R-D12).

NOTE (SKILL-B R-B7): This file exceeds ~400 lines because it serves two
curation layers (SPEC-05 Layer 1 and SPEC-06 Layer 2). Scheduled split:
  curation.py           → Layer 1 candidate review
  curation_pipeline.py  → Layer 2 proposal / approval / execution panels

Backend endpoints consumed:
  POST /api/curation/candidates/generate
  GET  /api/curation/candidates/{run_id}
  GET  /api/curation/evidence/{candidate_id}?run_id=
  POST /api/curation/propose
  GET  /api/curation/proposals/{run_id}
  POST /api/curation/proposals/{id}/approve
  POST /api/curation/proposals/{id}/confirm
  POST /api/curation/proposals/{id}/reject
  POST /api/curation/proposals/{id}/defer
  POST /api/curation/proposals/{id}/execute
"""

import os
from typing import Any

import httpx
import streamlit as st

from api.models.run import Phase
from ui.state import StateManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8000")
_REQUEST_TIMEOUT: float = 15.0

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
    "rejected": ":red[REJECTED]",
    "deferred": ":gray[DEFERRED]",
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


# ===========================================================================
# Layer 1 — Candidate Review (SPEC-05)
# ===========================================================================


def _render_trigger_section(run_id: str) -> None:
    """Render the candidate generation trigger button and inline count summary."""
    st.subheader("Generate Candidates")
    st.caption(
        "Runs all five deterministic detectors against the current Neo4j graph "
        "for this run.  Zero-LLM — results are fully reproducible.  Cached for "
        "5 minutes; re-click to force a fresh detection pass after the TTL expires."
    )

    if st.button(
        "Generate Candidates",
        type="primary",
        help="Runs exact-dup, probable-dup, canonical-violation, and structural-anomaly detectors.",
    ):
        with st.spinner("Running candidate detection…"):
            data, err = _post(
                "/api/curation/candidates/generate",
                {"run_id": run_id},
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
            st.success("Detection complete — no candidates found in this run.")
        else:
            st.success(f"Detection complete — {total} candidate(s) found.")

        if counts:
            st.markdown("**Candidates by detector type:**")
            cols = st.columns(max(len(counts), 1))
            for col, entry in zip(cols, counts):
                label = _TYPE_LABELS.get(
                    entry["candidate_type"], entry["candidate_type"]
                )
                col.metric(label, entry["count"])

        if sv:
            st.caption(f"Schema version: `{sv[:16]}…`")

        st.rerun()


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


def _render_candidate_group(group: dict[str, Any]) -> None:
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
                "Method": c.get("detection_method", ""),
                "Elements": _format_refs(c.get("involved_element_refs", [])),
                "Context": _format_context(c.get("collision_context", {})),
            }
            for c in candidates
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)


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

    if dedupe_keys:
        st.caption(f"Queried {len(dedupe_keys)} element key(s) from Qdrant.")

    if not chunks:
        st.info("No evidence chunks found. Qdrant may not have been indexed for this run.")
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


def _render_execute_panel(
    proposal: dict[str, Any], run_id: str, actor: str
) -> None:
    """Render the execute panel for an approved proposal.

    Two-step flow:
    1. 'Show Approval ID' calls POST /approve (idempotent — same inputs →
       same deterministic approval_id) and displays the token.
    2. User pastes the approval_id into the Execute form and submits.

    The execute outcome and diff_id form the inline audit trail entry.
    """
    pid = proposal["proposal_id"]

    st.markdown("**Execute Approved Proposal**")
    st.caption(
        "Step 1: Click 'Show Approval ID' to retrieve the approval token. "
        "Step 2: Paste it below and click Execute."
    )

    # Step 1: Retrieve approval_id (idempotent approve call).
    if st.button(
        "Show Approval ID",
        key=f"get_approval_{pid}",
        help="Calls the approve endpoint again (idempotent) to display the approval token.",
    ):
        data, err = _post(
            f"/api/curation/proposals/{pid}/approve",
            {"run_id": run_id, "actor": actor},
        )
        if err or data is None:
            st.error(f"Could not retrieve approval ID: {err or 'No response from API.'}")
        elif data.get("status") == "error":
            for e in data.get("errors", []):
                # 409 means already approved — that's fine, still show the key.
                if e.get("code") == "already_approved":
                    st.info("Proposal is already approved. Re-derive approval ID:")
                else:
                    st.error(f"[{e.get('code')}] {e.get('message')}")
        elif data.get("confirmation_required"):
            st.warning(
                "Proposal still awaiting Phase 2 confirmation — "
                "complete the confirm step first."
            )
        else:
            st.info("Approval ID (copy this):")
            st.code(data.get("approval_id", ""), language=None)

    # Step 2: Execute form.
    with st.form(key=f"execute_form_{pid}"):
        approval_id_input = st.text_input(
            "Approval ID",
            placeholder="Paste the approval ID from Step 1 above",
        )
        actor_input = st.text_input("Actor", value=actor)
        submitted = st.form_submit_button("Execute", type="primary")

    if submitted:
        if not approval_id_input.strip():
            st.error("Approval ID is required.")
        else:
            with st.spinner("Executing…"):
                data, err = _post(
                    f"/api/curation/proposals/{pid}/execute",
                    {
                        "run_id": run_id,
                        "approval_id": approval_id_input.strip(),
                        "actor": actor_input or actor,
                    },
                )
            if err or data is None:
                st.error(f"Execution failed: {err or 'No response from API.'}")
            elif data.get("status") == "error":
                for e in data.get("errors", []):
                    st.error(f"[{e.get('code')}] {e.get('message')}")
            else:
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
        else:
            st.caption(f"No actions available — proposal is {state}.")


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

    pending = sum(1 for p in proposals if p["state"] == "pending")
    approved = sum(1 for p in proposals if p["state"] == "approved")

    c1, c2, c3 = st.columns(3)
    c1.metric("Total", total)
    c2.metric("Pending", pending)
    c3.metric("Approved", approved)

    st.divider()

    for proposal in proposals:
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
# Main
# ===========================================================================


def main() -> None:
    """Curation page entry point, called by Streamlit on every script rerun."""
    st.set_page_config(
        page_title="neo4all — Curation",
        layout="wide",
        initial_sidebar_state="expanded",
    )

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

    st.title("Phase 4: Curation")
    st.caption(
        "Layer 1: Zero-LLM candidate detection.  "
        "Layer 2: Evidence review, governed proposal pipeline, and execution."
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
                _render_candidate_group(group)

    # --- Layer 2: Candidate detail, evidence, proposal form ---
    st.divider()
    _render_candidate_detail_section(groups, run_id, actor)

    st.divider()

    # --- Layer 2: Proposal queue ---
    _render_proposal_queue(run_id, actor)


if __name__ == "__main__":
    main()
