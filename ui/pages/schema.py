"""
ui/pages/schema.py — Phase 1: Domain Schema Definition (SPEC-02 S-02.6).

The user describes their knowledge domain in natural language. The AI proposes
node and edge types. The user reviews and edits the proposal in data tables.
On approval, the schema is locked and the run advances to Phase 2 (Ingestion).

Architecture rules enforced here
---------------------------------
- All st.session_state access via StateManager.get() only (CLAUDE.md §8).
- No business logic — no ID derivation, no Pydantic validation, no graph
  queries. All operations are delegated to the backend API (SKILL-B).
- API calls via httpx synchronous client (Streamlit is single-threaded).
- No imports from api/graph/, api/agents/, api/vector/, api/diff/, api/audit/.
  Only api.models.run is imported for the Phase enum (SKILL-B R-B3).

Workflow states
---------------
1. Empty    — no proposal yet; show domain description input.
2. Proposed — LLM proposal received; show editable node/edge tables.
3. Locked   — schema approved; show read-only tables + proceed button.

Startup reconciliation
----------------------
On every page load, if run_id is set but schema_version is not stored in
StateManager (e.g. after a browser refresh), the page silently queries
GET /api/schema/{run_id}. If the backend reports the schema as locked, the
UI state is updated to reflect the locked view — no user action required.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pandas as pd
import streamlit as st

from api.models.run import Phase
from ui.state import PANEL_READ, StateManager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8000")
_REQUEST_TIMEOUT_FAST: float = 5.0   # used for GET requests
_REQUEST_TIMEOUT_SLOW: float = 60.0  # used for POST /propose (LLM call)

# Named panel identifier used with StateManager panel mode tracking.
_PANEL_SCHEMA = "schema_editor"

# Minimum word count for the domain description before allowing a propose call.
_MIN_DOMAIN_WORDS: int = 10

# DataFrame column names — kept as constants so helpers and widget configs
# reference the same strings.
_NODE_COLS = [
    "node_class",
    "type",
    "primary_property",
    "qualifier",
    "additional_properties",
]
_EDGE_COLS = [
    "start_node_type",
    "end_node_type",
    "type",
    "primary_property",
    "qualifier",
    "additional_properties",
]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST to the backend API and return the parsed JSON response body.

    Returns the response dict for both success (2xx) and business-logic error
    (4xx) responses so callers can display structured error detail.
    Returns None only on connectivity or timeout failures.

    Args:
        path:    URL path appended to _API_BASE_URL (must start with "/").
        payload: JSON-serialisable request body.
    """
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_SLOW) as client:
            response = client.post(f"{_API_BASE_URL}{path}", json=payload)
            return response.json()  # type: ignore[no-any-return]
    except Exception:
        return None


def _get(path: str) -> dict[str, Any] | None:
    """GET from the backend API and return parsed JSON on success, else None."""
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_FAST) as client:
            response = client.get(f"{_API_BASE_URL}{path}")
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DataFrame conversion helpers
# UI-layer formatting only — not business logic.
# additional_properties is a list[str] in the API contract; it is displayed
# as a comma-separated string in the data editor for usability.
# ---------------------------------------------------------------------------


def _nodes_to_df(nodes: list[dict[str, Any]]) -> pd.DataFrame:
    """Convert an API node list to an editable DataFrame."""
    if not nodes:
        return pd.DataFrame(columns=_NODE_COLS)
    return pd.DataFrame(
        [
            {
                "node_class": n.get("node_class") or "",
                "type": n.get("type") or "",
                "primary_property": n.get("primary_property") or "",
                "qualifier": n.get("qualifier") or "",
                "additional_properties": ", ".join(
                    n.get("additional_properties") or []
                ),
            }
            for n in nodes
        ],
        columns=_NODE_COLS,
    )


def _edges_to_df(edges: list[dict[str, Any]]) -> pd.DataFrame:
    """Convert an API edge list to an editable DataFrame."""
    if not edges:
        return pd.DataFrame(columns=_EDGE_COLS)
    return pd.DataFrame(
        [
            {
                "start_node_type": e.get("start_node_type") or "",
                "end_node_type": e.get("end_node_type") or "",
                "type": e.get("type") or "",
                "primary_property": e.get("primary_property") or "",
                "qualifier": e.get("qualifier") or "",
                "additional_properties": ", ".join(
                    e.get("additional_properties") or []
                ),
            }
            for e in edges
        ],
        columns=_EDGE_COLS,
    )


def _df_to_nodes(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert a data editor DataFrame back to API-ready node dicts.

    Filters out blank rows (type field empty). Parses the
    additional_properties comma-separated string back to a list.
    """
    result: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        node_type = str(row.get("type", "")).strip()
        if not node_type:
            continue  # skip blank rows added by the user
        extras_raw = str(row.get("additional_properties", ""))
        extras = [p.strip() for p in extras_raw.split(",") if p.strip()]
        result.append(
            {
                "node_class": str(row.get("node_class", "")).strip(),
                "type": node_type,
                "primary_property": str(row.get("primary_property", "")).strip(),
                "qualifier": str(row.get("qualifier", "")).strip() or None,
                "additional_properties": extras,
            }
        )
    return result


def _df_to_edges(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert a data editor DataFrame back to API-ready edge dicts.

    Filters out blank rows (type field empty).
    """
    result: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        edge_type = str(row.get("type", "")).strip()
        if not edge_type:
            continue
        extras_raw = str(row.get("additional_properties", ""))
        extras = [p.strip() for p in extras_raw.split(",") if p.strip()]
        result.append(
            {
                "start_node_type": str(row.get("start_node_type", "")).strip(),
                "end_node_type": str(row.get("end_node_type", "")).strip(),
                "type": edge_type,
                "primary_property": str(row.get("primary_property", "")).strip() or None,
                "qualifier": str(row.get("qualifier", "")).strip() or None,
                "additional_properties": extras,
            }
        )
    return result


# ---------------------------------------------------------------------------
# Shared UI helpers
# ---------------------------------------------------------------------------


def _word_count(text: str) -> int:
    """Return the number of whitespace-delimited words in text."""
    return len(text.split())


def _show_api_errors(result: dict[str, Any]) -> None:
    """Display structured error detail from a BaseResponse payload."""
    errors: list[dict[str, Any]] = result.get("errors", [])
    if not errors:
        st.error("The API returned an error with no detail. Check backend logs.")
        return
    for err in errors:
        code: str = err.get("code", "error")
        message: str = err.get("message", "An unknown error occurred.")
        field: str | None = err.get("field")
        label = f"[{code}] {field}: {message}" if field else f"[{code}] {message}"
        st.error(label)


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------


def _reconcile_backend_state(state: StateManager, run_id: str) -> None:
    """Silently sync UI state with backend schema lock status.

    Called once per page load when run_id is set but schema_version is not
    yet recorded in StateManager (e.g. after a browser refresh). If the
    backend reports the schema as locked, updates StateManager so the locked
    view is shown without requiring the user to re-approve.

    Never raises — connectivity failures are silently swallowed (the page
    falls back to the domain input form).

    Args:
        state:  Live StateManager instance.
        run_id: Governed run identifier for the current session.
    """
    data = _get(f"/api/schema/{run_id}")
    if data is None or not data.get("locked"):
        return

    sv: dict[str, Any] = data.get("schema_version") or {}
    version_hash: str = sv.get("version_hash", "")
    if not version_hash:
        return

    nodes: list[dict[str, Any]] = sv.get("nodes") or []
    edges: list[dict[str, Any]] = sv.get("edges") or []

    state.set_proposed_schema(nodes, edges)
    state.set_schema_version(version_hash)
    state.set_panel_mode(_PANEL_SCHEMA, PANEL_READ)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _handle_propose(
    state: StateManager, domain_description: str, model: str | None = None
) -> None:
    """Handle the Propose Schema button click.

    Validates the domain description, calls POST /api/schema/propose, stores
    the result in StateManager, and triggers a rerun to show the editor.

    Args:
        state:              Live StateManager instance.
        domain_description: The user's free-text domain description.
        model:              Optional OpenRouter model override for the LLM call.
    """
    word_count = _word_count(domain_description.strip())
    if word_count < _MIN_DOMAIN_WORDS:
        st.error(
            f"Domain description is too short ({word_count} word"
            f"{'s' if word_count != 1 else ''}). "
            f"Please provide at least {_MIN_DOMAIN_WORDS} words so the AI "
            "has enough context to generate a meaningful schema."
        )
        return

    run_id = state.run_id
    payload: dict[str, Any] = {
        "run_id": run_id,
        "domain_description": domain_description.strip(),
    }
    if model and model.strip():
        payload["model"] = model.strip()

    with st.spinner(
        "Calling LLM to generate schema proposal — this may take 10–30 seconds…"
    ):
        result = _post("/api/schema/propose", payload)

    if result is None:
        st.error(
            "Cannot reach the backend API. "
            "Ensure the API server is running and retry."
        )
        return

    if result.get("status") == "error":
        _show_api_errors(result)
        return

    proposed_nodes: list[dict[str, Any]] = result.get("nodes") or []
    proposed_edges: list[dict[str, Any]] = result.get("edges") or []

    if not proposed_nodes:
        st.error(
            "The API returned an empty node list. "
            "Try rephrasing or expanding your domain description."
        )
        return

    state.set_proposed_schema(proposed_nodes, proposed_edges)
    st.rerun()


def _handle_approve(
    state: StateManager,
    edited_nodes_df: pd.DataFrame,
    edited_edges_df: pd.DataFrame,
) -> None:
    """Handle the Approve & Lock Schema button click.

    Converts the editor DataFrames to API-ready dicts, calls
    POST /api/schema/approve, and on success locks the schema in StateManager.

    Args:
        state:           Live StateManager instance.
        edited_nodes_df: Current state of the node data editor.
        edited_edges_df: Current state of the edge data editor.
    """
    nodes = _df_to_nodes(edited_nodes_df)
    edges = _df_to_edges(edited_edges_df)

    if not nodes:
        st.error(
            "The schema must contain at least one node type. "
            "Add a node row before approving."
        )
        return

    run_id = state.run_id
    payload: dict[str, Any] = {
        "run_id": run_id,
        "schema_data": {"nodes": nodes, "edges": edges},
    }

    with st.spinner("Locking schema…"):
        result = _post("/api/schema/approve", payload)

    if result is None:
        st.error(
            "Cannot reach the backend API. "
            "Ensure the API server is running and retry."
        )
        return

    if result.get("status") == "error":
        _show_api_errors(result)
        return

    sv: dict[str, Any] = result.get("schema_version") or {}
    version_hash: str = sv.get("version_hash", "")

    # Store the API-approved definitions (server-validated) back into
    # StateManager so the locked view reflects exactly what was approved.
    approved_nodes: list[dict[str, Any]] = sv.get("nodes") or nodes
    approved_edges: list[dict[str, Any]] = sv.get("edges") or edges
    state.set_proposed_schema(approved_nodes, approved_edges)
    state.set_schema_version(version_hash)
    state.set_panel_mode(_PANEL_SCHEMA, PANEL_READ)
    st.rerun()


# ---------------------------------------------------------------------------
# Panel: Step 1 — domain description input
# ---------------------------------------------------------------------------


def _render_propose_section(state: StateManager, *, has_proposal: bool) -> None:
    """Render the domain description text area and Propose button.

    When a proposal is already loaded (has_proposal=True), this section is
    collapsed into a read-only expander so the editor can share the page.

    Args:
        state:        Live StateManager instance.
        has_proposal: True when a proposal is already held in StateManager.
    """
    if has_proposal:
        with st.expander("Step 1: Domain Description — completed", expanded=False):
            st.caption(
                "A schema proposal was generated for this domain. "
                "Review and edit the tables below, or click **Re-propose** "
                "to discard the proposal and start over."
            )
        return

    st.subheader("Step 1: Describe your domain")
    st.write(
        "Describe the knowledge domain you want to model as a graph. "
        "The more specific you are — including the kinds of entities, "
        "documents, events, and relationships that matter — the better "
        "the AI's schema proposal will be."
    )
    st.caption(
        "_Example: 'A biomedical research domain covering clinical trials, "
        "drugs, patient cohorts, adverse events, and regulatory approvals "
        "in oncology.'_"
    )

    with st.form("schema_propose_form"):
        domain_description: str = st.text_area(
            "Domain Description",
            height=140,
            placeholder=(
                "Describe your domain in detail. Include the types of entities, "
                "relationships, and documents you expect to extract from "
                "source material. Minimum 10 words."
            ),
            help=(
                f"Minimum {_MIN_DOMAIN_WORDS} words. Be specific — context "
                "improves the quality of the schema proposal."
            ),
            label_visibility="collapsed",
        )
        schema_model: str = st.text_input(
            "LLM Model (optional)",
            value="openai/gpt-4o-mini",
            help=(
                "OpenRouter model ID for the schema proposal. "
                "Leave as default or enter any supported model "
                "(e.g. anthropic/claude-sonnet-4, google/gemini-2.0-flash-001)."
            ),
        )
        submitted = st.form_submit_button(
            "Propose Schema",
            type="primary",
            use_container_width=True,
        )

    if submitted:
        _handle_propose(state, domain_description, model=schema_model)


# ---------------------------------------------------------------------------
# Panel: Step 2 — editable proposal tables
# ---------------------------------------------------------------------------


def _render_editor_section(state: StateManager) -> None:
    """Render editable node and edge type tables with an approve button.

    Widget keys include the proposal_version suffix so that each new proposal
    gets a fresh data_editor, clearing any edits from a prior proposal.

    Args:
        state: Live StateManager instance.
    """
    nodes: list[dict[str, Any]] = state.proposed_nodes or []
    edges: list[dict[str, Any]] = state.proposed_edges or []
    v: int = state.proposal_version  # widget key suffix

    node_df = _nodes_to_df(nodes)
    edge_df = _edges_to_df(edges)

    st.info(
        "Review the AI-generated schema below. "
        "Edit any cell directly, add rows with the **+** button at the bottom, "
        "or select rows with the checkbox to delete them. "
        "When satisfied, click **Approve & Lock Schema**.",
        icon="✏️",
    )

    # ── Node Types ──────────────────────────────────────────────────────────
    st.subheader(f"Node Types — {len(nodes)} proposed")
    st.caption(
        "Each row defines a Neo4j label. "
        "**primary_property** (★) is the field used for entity deduplication "
        "across documents — choose carefully. "
        "**additional_properties**: comma-separated list of extra fields."
    )

    edited_nodes_df: pd.DataFrame = st.data_editor(
        node_df,
        key=f"node_editor_{v}",
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "node_class": st.column_config.TextColumn(
                "Class",
                width="small",
                help="High-level grouping: Entity, Event, Concept, Document, Location, Asset, Metric, Role.",
            ),
            "type": st.column_config.TextColumn(
                "Type (Neo4j label)",
                width="medium",
                help="PascalCase singular noun — this becomes the Neo4j label.",
            ),
            "primary_property": st.column_config.TextColumn(
                "Primary Property ★",
                width="medium",
                help=(
                    "Identifies instances for deduplication. "
                    "Two mentions of the same entity merge when their "
                    "primary_property values match."
                ),
            ),
            "qualifier": st.column_config.TextColumn(
                "Qualifier",
                width="small",
                help="Optional sub-type discriminator (e.g. 'government', 'academic').",
            ),
            "additional_properties": st.column_config.TextColumn(
                "Extra Properties",
                width="large",
                help="Comma-separated list of other properties to capture.",
            ),
        },
    )

    st.divider()

    # ── Edge Types ──────────────────────────────────────────────────────────
    st.subheader(f"Edge Types — {len(edges)} proposed")
    st.caption(
        "Each row defines a Neo4j relationship type. "
        "**start_node_type** and **end_node_type** must match a **Type** "
        "value in the Node Types table. "
        "**additional_properties**: comma-separated list."
    )

    edited_edges_df: pd.DataFrame = st.data_editor(
        edge_df,
        key=f"edge_editor_{v}",
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "start_node_type": st.column_config.TextColumn(
                "From Node Type",
                width="medium",
                help="Must match a Type value from the Node Types table.",
            ),
            "end_node_type": st.column_config.TextColumn(
                "To Node Type",
                width="medium",
                help="Must match a Type value from the Node Types table.",
            ),
            "type": st.column_config.TextColumn(
                "Rel Type",
                width="medium",
                help="SCREAMING_SNAKE_CASE relationship name (e.g. WORKS_FOR).",
            ),
            "primary_property": st.column_config.TextColumn(
                "Primary Property",
                width="small",
                help=(
                    "Optional. Distinguishes multiple edges of the same type "
                    "between the same node pair."
                ),
            ),
            "qualifier": st.column_config.TextColumn(
                "Qualifier",
                width="small",
                help="Optional sub-type or temporal context (e.g. 'current', 'former').",
            ),
            "additional_properties": st.column_config.TextColumn(
                "Extra Properties",
                width="large",
                help="Comma-separated list of relationship properties to capture.",
            ),
        },
    )

    st.divider()

    col_approve, col_repropose = st.columns([3, 1])
    with col_approve:
        if st.button(
            "Approve & Lock Schema",
            type="primary",
            use_container_width=True,
        ):
            _handle_approve(state, edited_nodes_df, edited_edges_df)
    with col_repropose:
        if st.button("Re-propose", use_container_width=True, help="Discard this proposal and enter a new domain description."):
            state.clear_proposed_schema()
            st.rerun()


# ---------------------------------------------------------------------------
# Panel: locked schema — read-only display
# ---------------------------------------------------------------------------


def _render_locked_view(state: StateManager) -> None:
    """Render the locked schema in read-only mode.

    Displays the version hash, read-only node and edge tables using
    st.dataframe (not st.data_editor — locked schemas must not be editable),
    and a Proceed button that advances the phase.

    Args:
        state: Live StateManager instance.
    """
    st.success(
        f"Schema locked — version hash `{state.schema_version}`",
        icon="✅",
    )
    st.caption(
        "The schema is now immutable for this run. "
        "Node and edge types below are read-only. "
        "Click **Proceed** to move to Phase 2."
    )
    st.divider()

    nodes: list[dict[str, Any]] = state.proposed_nodes or []
    edges: list[dict[str, Any]] = state.proposed_edges or []

    col_nodes, col_edges = st.columns(2)

    with col_nodes:
        st.subheader(f"Node Types ({len(nodes)})")
        if nodes:
            st.dataframe(
                _nodes_to_df(nodes),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No node types recorded.")

    with col_edges:
        st.subheader(f"Edge Types ({len(edges)})")
        if edges:
            st.dataframe(
                _edges_to_df(edges),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No edge types recorded.")

    st.divider()

    # Guard: only show the proceed button when the phase is still SCHEMA.
    # If the user navigates here via Streamlit multipage sidebar after
    # already advancing past Phase 1, show a status message instead.
    if state.phase != Phase.SCHEMA:
        st.info(
            "Schema is locked and you have already proceeded past Phase 1.",
            icon="✅",
        )
        return

    def _do_advance() -> None:
        """on_click callback — runs BEFORE the next render cycle."""
        s = StateManager.get()
        if s.phase == Phase.SCHEMA:
            s.advance_phase(Phase.INGESTION)

    st.button(
        "Proceed to Phase 2: Document Ingestion →",
        type="primary",
        on_click=_do_advance,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Phase 1 entry point. Called by ui/app.py when phase == Phase.SCHEMA."""
    # All session state access flows through StateManager (CLAUDE.md §8).
    state = StateManager.get()

    st.title("Phase 1 — Domain Schema Definition")
    st.write(
        "Define the node types and relationship types for your knowledge graph. "
        "Describe your domain and the AI will propose a schema for your review. "
        "You can edit the proposal before locking it for this run."
    )

    run_id = state.run_id
    if not run_id:
        st.error(
            "No active session. "
            "Return to Phase 0 to initialise a session before defining a schema."
        )
        return

    # ── Startup reconciliation ───────────────────────────────────────────────
    # If schema_version is not in StateManager (e.g. after a browser refresh
    # mid-session), silently check the backend and restore locked state.
    if state.schema_version is None:
        _reconcile_backend_state(state, run_id)

    # ── Route to the correct view based on current state ────────────────────
    schema_locked = state.schema_version is not None

    if schema_locked:
        _render_locked_view(state)
    elif state.proposed_nodes is not None:
        # Proposal received — show collapsed describe section + editable tables.
        _render_propose_section(state, has_proposal=True)
        st.divider()
        st.subheader("Step 2: Review & Edit Proposed Schema")
        _render_editor_section(state)
    else:
        # No proposal yet — show the domain description form.
        _render_propose_section(state, has_proposal=False)


if __name__ == "__main__":
    main()
