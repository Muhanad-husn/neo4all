"""
ui/app.py — Streamlit application entry point.

Responsibilities:
  - Phase 0 screen: collect Neo4j URI/credentials and OpenRouter API key.
  - Sidebar: display current run state across all phases.
  - Route to the correct phase screen based on StateManager.phase.

Architecture rules enforced here:
  - All st.session_state access via StateManager.get() only (CLAUDE.md §8).
  - No business logic — no API calls, no graph queries, no validation
    beyond "field is not empty" (SKILL-B).
  - No direct imports from api/graph/, api/agents/, api/vector/,
    api/diff/, or api/audit/ (SKILL-B R-B3).

Phase routing (SPEC-01 implements Phase 0 only):
  Phase.INIT       → Phase 0 credentials form
  Phase.SCHEMA     → placeholder (SPEC-02)
  Phase.INGESTION  → placeholder (SPEC-03)
  Phase.EXTRACTION → placeholder (SPEC-04)
  Phase.CURATION   → placeholder (SPEC-05+)
"""

from datetime import UTC, datetime, timedelta

import streamlit as st

from api.models.run import Phase
from ui.state import StateManager

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def _render_sidebar(state: StateManager) -> None:
    """Render the persistent run-state sidebar.

    Shows phase progression indicators and a compact run summary. Visible
    on every page rerun. No state mutations here — read-only presentation.
    """
    with st.sidebar:
        st.title("neo4all")
        st.caption("AI-Powered Graph Curation")
        st.divider()

        # Phase progression indicators.
        st.subheader("Phases")
        current_phase = state.phase
        phase_labels = {
            Phase.INIT: "Session Init",
            Phase.SCHEMA: "Schema",
            Phase.INGESTION: "Ingestion",
            Phase.EXTRACTION: "Extraction",
            Phase.CURATION: "Curation",
        }
        for p in Phase:
            if p.value < current_phase.value:
                marker = ":green[✓]"
            elif p == current_phase:
                marker = ":blue[→]"
            else:
                marker = ":gray[○]"
            st.markdown(f"{marker} **{p.value}** {phase_labels[p]}")

        st.divider()

        # Compact run summary.
        st.subheader("Run State")
        run_id = state.run_id
        if run_id:
            st.caption("Run ID")
            st.code(run_id[:16] + "…", language=None)

            if state.neo4j_uri:
                st.caption("Neo4j URI")
                st.write(state.neo4j_uri)

            if state.neo4j_user:
                st.caption("User")
                st.write(state.neo4j_user)

            if state.schema_version:
                st.caption("Schema Version")
                st.write(state.schema_version)

            # Phase timing: elapsed since INIT start.
            timings = state.phase_start_times
            init_iso = timings.get(Phase.INIT.value)
            if init_iso:
                elapsed = datetime.now(UTC) - datetime.fromisoformat(init_iso)
                st.caption("Session elapsed")
                st.write(_fmt_duration(elapsed))
        else:
            st.info("No active run.\nComplete Phase 0 to begin.")


# ---------------------------------------------------------------------------
# Phase 0: Session Initialization
# ---------------------------------------------------------------------------


def _render_phase_init(state: StateManager) -> None:
    """Phase 0: collect credentials and initialize the session.

    Uses st.form so all fields are submitted atomically. No API calls are
    made here — credentials are stored in StateManager for use by later
    phases when constructing API requests (SKILL-B).
    """
    st.title("Phase 0 — Session Initialization")
    st.write(
        "Enter your connection details to start a new extraction and curation session. "
        "Credentials are held in server-side session memory and never written to disk or logs."
    )

    with st.form("phase_0_init_form"):
        st.subheader("Neo4j Aura")
        neo4j_uri = st.text_input(
            "URI",
            value=state.neo4j_uri or "",
            placeholder="neo4j+s://xxxxxxxx.databases.neo4j.io",
            help="Your Neo4j Aura connection URI (starts with neo4j+s://).",
        )
        neo4j_user = st.text_input(
            "Username",
            value=state.neo4j_user or "",
            placeholder="neo4j",
            help="Neo4j database username.",
        )
        neo4j_password = st.text_input(
            "Password",
            type="password",
            placeholder="Enter password",
            help="Neo4j database password. Not stored to disk or logged.",
        )

        st.subheader("LLM Gateway")
        openrouter_api_key = st.text_input(
            "OpenRouter API Key",
            type="password",
            placeholder="sk-or-…",
            help="OpenRouter API key for LLM access. Not stored to disk or logged.",
        )

        submitted = st.form_submit_button("Initialize Session", type="primary")

    # Handle submission outside the `with st.form` block so that st.error()
    # and st.rerun() render correctly.
    if submitted:
        errors: list[str] = []
        if not neo4j_uri.strip():
            errors.append("Neo4j URI is required.")
        if not neo4j_user.strip():
            errors.append("Neo4j Username is required.")
        if not neo4j_password:
            errors.append("Neo4j Password is required.")
        if not openrouter_api_key:
            errors.append("OpenRouter API Key is required.")

        if errors:
            for msg in errors:
                st.error(msg)
        else:
            state.initialize_session(
                neo4j_uri=neo4j_uri.strip(),
                neo4j_user=neo4j_user.strip(),
                neo4j_password=neo4j_password,
                openrouter_api_key=openrouter_api_key,
            )
            try:
                state.advance_phase(Phase.SCHEMA)
            except ValueError as exc:
                st.error(f"Phase transition error: {exc}")
                return
            st.rerun()


# ---------------------------------------------------------------------------
# Placeholder screens for phases implemented in later increments
# ---------------------------------------------------------------------------


def _render_phase_placeholder(phase: Phase, spec_ref: str) -> None:
    """Placeholder rendered for phases not yet implemented in SPEC-01.

    Args:
        phase:    The Phase being rendered.
        spec_ref: Reference to the spec that implements this phase.
    """
    titles = {
        Phase.SCHEMA: "Phase 1 — Domain Schema Definition",
        Phase.INGESTION: "Phase 2 — Document Ingestion & Chunking",
        Phase.EXTRACTION: "Phase 3 — AI-Assisted Extraction",
        Phase.CURATION: "Phase 4 — Curation",
    }
    st.title(titles.get(phase, f"Phase {phase.value}"))
    st.info(
        f"This phase is not yet implemented. "
        f"It will be available after **{spec_ref}** is complete.",
        icon="ℹ",
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _fmt_duration(d: timedelta) -> str:
    """Format a timedelta as a human-readable string."""
    total_seconds = int(d.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def main() -> None:
    """Application entry point, called by Streamlit on every script rerun."""
    st.set_page_config(
        page_title="neo4all",
        page_icon=None,
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # StateManager.get() seeds defaults on first load and returns a live view.
    state = StateManager.get()

    _render_sidebar(state)

    phase = state.phase
    if phase == Phase.INIT:
        _render_phase_init(state)
    elif phase == Phase.SCHEMA:
        from ui.pages import schema as schema_page
        schema_page.main()
    elif phase == Phase.INGESTION:
        _render_phase_placeholder(phase, "SPEC-03")
    elif phase == Phase.EXTRACTION:
        _render_phase_placeholder(phase, "SPEC-04")
    elif phase == Phase.CURATION:
        _render_phase_placeholder(phase, "SPEC-05 through SPEC-07")
    else:
        st.error(f"Unknown phase: {phase!r}. This is a bug.")


if __name__ == "__main__":
    main()
