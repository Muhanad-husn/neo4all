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
from pathlib import Path

import streamlit as st

from api.models.run import Phase
from ui.state import StateManager

# ---------------------------------------------------------------------------
# .env credential bridge (Phase 0 → API)
# ---------------------------------------------------------------------------

# Base URL for API requests — matches the default uvicorn bind in api/main.py.
_API_BASE_URL = "http://localhost:8000"

# Mapping from Phase 0 form fields to the env-var key names that may appear in
# .env. The first match wins during line-by-line replacement; all aliases are
# checked so both Aura-generated and canonical names are handled.
_NEO4J_URI_KEYS = {"NEO4J_URI", "NEO4J_DEV_URI"}
_NEO4J_USER_KEYS = {"NEO4J_USERNAME", "NEO4J_DEV_USER"}
_NEO4J_PASSWORD_KEYS = {"NEO4J_PASSWORD", "NEO4J_DEV_PASSWORD"}
_OPENROUTER_KEY_KEYS = {"OPENROUTER_API_KEY"}


def _write_env_credentials(
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    openrouter_api_key: str,
) -> None:
    """Update credential values in ``.env``, preserving all other content.

    If ``.env`` does not exist, ``.env.example`` is used as a template.
    Lines matching known credential key names have their values replaced;
    all other lines (comments, non-credential variables) pass through
    unchanged.

    Raises:
        OSError: If neither ``.env`` nor ``.env.example`` can be read, or if
                 the file cannot be written.
    """
    env_path = Path(".env")
    if env_path.exists():
        original = env_path.read_text(encoding="utf-8")
    else:
        example_path = Path(".env.example")
        if example_path.exists():
            original = example_path.read_text(encoding="utf-8")
        else:
            raise OSError("Neither .env nor .env.example found")

    # Build a lookup: env-var key → new value (only for credential keys).
    replacements: dict[str, str] = {}
    for key in _NEO4J_URI_KEYS:
        replacements[key] = neo4j_uri
    for key in _NEO4J_USER_KEYS:
        replacements[key] = neo4j_user
    for key in _NEO4J_PASSWORD_KEYS:
        replacements[key] = neo4j_password
    for key in _OPENROUTER_KEY_KEYS:
        replacements[key] = openrouter_api_key

    output_lines: list[str] = []
    for line in original.splitlines(keepends=True):
        stripped = line.lstrip()
        # Skip comments and blank lines — pass through unchanged.
        if stripped.startswith("#") or "=" not in stripped:
            output_lines.append(line)
            continue

        key = stripped.split("=", 1)[0].strip()
        if key in replacements:
            # Preserve the original key name and any leading whitespace.
            prefix = line[: line.index(key)]
            output_lines.append(f"{prefix}{key}={replacements[key]}\n")
        else:
            output_lines.append(line)

    env_path.write_text("".join(output_lines), encoding="utf-8")


def _notify_api_reload() -> bool:
    """Tell the running API to reload its configuration.

    Fire-and-forget: returns True on success, False on any failure.
    Failures are expected when the API server is not running.
    """
    try:
        import httpx

        resp = httpx.Client(timeout=5.0).post(f"{_API_BASE_URL}/api/config/reload")
        return resp.status_code == 200  # noqa: TRY300
    except Exception:
        return False

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
        "Credentials are saved to the local `.env` file so the API backend can use them."
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
            help="Neo4j database password. Saved to `.env`; never logged.",
        )

        st.subheader("LLM Gateway")
        openrouter_api_key = st.text_input(
            "OpenRouter API Key",
            type="password",
            placeholder="sk-or-…",
            help="OpenRouter API key for LLM access. Saved to `.env`; never logged.",
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
            # Persist credentials to .env so the API backend can read them.
            try:
                _write_env_credentials(
                    neo4j_uri=neo4j_uri.strip(),
                    neo4j_user=neo4j_user.strip(),
                    neo4j_password=neo4j_password,
                    openrouter_api_key=openrouter_api_key,
                )
            except OSError as exc:
                st.warning(f"Could not save credentials to .env: {exc}")

            # Tell the running API to reload its configuration.
            if _notify_api_reload():
                st.info("API configuration reloaded with new credentials.")

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
        from ui.pages import ingestion as ingestion_page
        ingestion_page.main()
    elif phase == Phase.EXTRACTION:
        from ui.pages import extraction as extraction_page
        extraction_page.main()
    elif phase == Phase.CURATION:
        from ui.pages import curation as curation_page
        curation_page.main()
    else:
        st.error(f"Unknown phase: {phase!r}. This is a bug.")


if __name__ == "__main__":
    main()
