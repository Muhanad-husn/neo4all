"""
ui/app.py — Streamlit application entry point.

Responsibilities:
  - Phase 0 screen: collect Neo4j URI/credentials and OpenRouter API key.
  - Sidebar: display current run state, phase indicators, and view selector.
  - Route to the correct phase screen based on StateManager.phase.

Architecture rules enforced here:
  - All st.session_state access via StateManager.get() only (CLAUDE.md §8).
  - No business logic — no API calls, no graph queries, no validation
    beyond "field is not empty" (SKILL-B).
  - No direct imports from api/graph/, api/agents/, api/vector/,
    api/diff/, or api/audit/ (SKILL-B R-B3).

Phase routing (simple if/elif — no st.navigation()):
  Phase.INIT       → Phase 0 credentials form (inline)
  Phase.SCHEMA     → ui/pages/schema.py
  Phase.INGESTION  → ui/pages/ingestion.py
  Phase.EXTRACTION → ui/pages/extraction.py
  Phase.CURATION   → ui/pages/curation.py

Utility views (sidebar selector, available after Phase 0):
  Dashboard        → ui/pages/dashboard.py
  Graph Explorer   → ui/pages/graph_explorer.py  (Phase 4+ only)
"""

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import streamlit as st
from PIL import Image

from api.models.run import Phase
from ui.components.activity_feed import render_activity_feed
from ui.state import StateManager

# ---------------------------------------------------------------------------
# Logo paths (resolved relative to this file so they work regardless of cwd)
# ---------------------------------------------------------------------------
_DOCS_DIR = Path(__file__).parent.parent / "docs"
_LOGO_DARK = _DOCS_DIR / "dark-mode-logo.png"
_LOGO_LIGHT = _DOCS_DIR / "light-mode-logo.png"

# ---------------------------------------------------------------------------
# .env credential bridge (Phase 0 → API)
# ---------------------------------------------------------------------------

# Base URL for API requests. In Docker the UI container reaches the API via
# the service name (API_BASE_URL=http://api:8000); locally falls back to
# localhost.
_API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8000")

# Mapping from Phase 0 form fields to the env-var key names that may appear in
# .env. The first match wins during line-by-line replacement; all aliases are
# checked so both Aura-generated and canonical names are handled.
_NEO4J_URI_KEYS = {"NEO4J_URI", "NEO4J_DEV_URI"}
_NEO4J_USER_KEYS = {"NEO4J_USERNAME", "NEO4J_DEV_USER"}
_NEO4J_PASSWORD_KEYS = {"NEO4J_PASSWORD", "NEO4J_DEV_PASSWORD"}
_OPENROUTER_KEY_KEYS = {"OPENROUTER_API_KEY"}

# Session-state key for the sidebar view selector.
# Prefixed _nav_ to distinguish from _sm_ (StateManager) keys.
_K_VIEW_OVERRIDE = "_nav_view_override"

# Session-state key to avoid repeated restore attempts on every Streamlit rerun.
_K_RESTORE_ATTEMPTED = "_nav_restore_attempted"

# Session-state key storing restore diagnostics for Phase 0 display.
_K_RESTORE_RESULT = "_nav_restore_result"

# Session-state key flagging degraded session persistence (save failures).
_K_SAVE_DEGRADED = "_nav_save_degraded"


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
            # No template — generate minimal credential-only .env.
            lines = []
            for k in _NEO4J_URI_KEYS:
                lines.append(f"{k}={neo4j_uri}\n")
                break  # one canonical key is enough
            for k in _NEO4J_USER_KEYS:
                lines.append(f"{k}={neo4j_user}\n")
                break
            for k in _NEO4J_PASSWORD_KEYS:
                lines.append(f"{k}={neo4j_password}\n")
                break
            for k in _OPENROUTER_KEY_KEYS:
                lines.append(f"{k}={openrouter_api_key}\n")
                break
            env_path.write_text("".join(lines), encoding="utf-8")
            return

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


def _read_env_credentials() -> dict[str, str]:
    """Read credential values from the ``.env`` file or ``os.environ``.

    Tries the ``.env`` file first (local development).  If the file does not
    exist (Docker: credentials are injected as env vars via ``env_file``),
    falls back to ``os.environ``.

    Returns:
        Dict with keys ``neo4j_uri``, ``neo4j_user``, ``neo4j_password``,
        ``openrouter_api_key``.  Missing values are empty strings.
        Never raises.
    """
    # Try .env file first (local dev).
    parsed: dict[str, str] = {}
    env_path = Path(".env")
    if env_path.exists():
        try:
            content = env_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                parsed[key.strip()] = value.strip()
        except OSError:
            pass

    # Fall back to os.environ for any keys not found in .env (Docker).
    def _first_match(key_set: set[str]) -> str:
        # Check parsed .env first, then os.environ.
        for k in key_set:
            if k in parsed and parsed[k]:
                return parsed[k]
        for k in key_set:
            val = os.environ.get(k, "")
            if val:
                return val
        return ""

    return {
        "neo4j_uri": _first_match(_NEO4J_URI_KEYS),
        "neo4j_user": _first_match(_NEO4J_USER_KEYS),
        "neo4j_password": _first_match(_NEO4J_PASSWORD_KEYS),
        "openrouter_api_key": _first_match(_OPENROUTER_KEY_KEYS),
    }


def _derive_user_hash(neo4j_uri: str, neo4j_user: str) -> str:
    """Derive a deterministic user_hash for session key scoping.

    Uses SHA-256(neo4j_uri + NUL + neo4j_user) to prevent collisions
    between users with the same name on different Aura instances.
    Follows the project's null-byte separator convention (CLAUDE.md §5).
    """
    raw = f"{neo4j_uri}\x00{neo4j_user}".encode()
    return hashlib.sha256(raw).hexdigest()


def _notify_api_reload(
    *,
    neo4j_uri: str = "",
    neo4j_user: str = "",
    neo4j_password: str = "",
    openrouter_api_key: str = "",
) -> bool:
    """Tell the running API to reload its configuration.

    Credentials are sent in the request body so the API can inject them
    into its process environment.  This is necessary in Docker where the
    UI and API containers have separate filesystems and cannot share a
    ``.env`` file.

    Fire-and-forget: returns True on success, False on any failure.
    Failures are expected when the API server is not running.
    """
    try:
        import httpx

        payload: dict[str, str] = {}
        if neo4j_uri:
            payload["neo4j_uri"] = neo4j_uri
        if neo4j_user:
            payload["neo4j_user"] = neo4j_user
        if neo4j_password:
            payload["neo4j_password"] = neo4j_password
        if openrouter_api_key:
            payload["openrouter_api_key"] = openrouter_api_key

        resp = httpx.Client(timeout=2.0).post(
            f"{_API_BASE_URL}/api/config/reload",
            json=payload,
        )
        return resp.status_code == 200  # noqa: TRY300
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Session persistence (restore / save / new-session)
# ---------------------------------------------------------------------------


def _try_restore_session(state: StateManager) -> bool:
    """Attempt to restore a previously saved session from Redis via the API.

    Called once per Streamlit process when ``phase == INIT`` and
    ``run_id is None`` (i.e., a completely fresh bootstrap).

    Returns True if a session was successfully restored, False otherwise.
    Stores diagnostic info in ``st.session_state[_K_RESTORE_RESULT]`` so
    Phase 0 can display a meaningful message on failure.
    """
    def _fail(reason: str) -> bool:
        st.session_state[_K_RESTORE_RESULT] = {
            "attempted": True,
            "success": False,
            "reason": reason,
        }
        return False

    try:
        import httpx

        creds = _read_env_credentials()
        neo4j_uri = creds["neo4j_uri"]
        neo4j_user = creds["neo4j_user"]
        neo4j_password = creds["neo4j_password"]
        openrouter_api_key = creds["openrouter_api_key"]

        # Cannot restore without at least URI and user.
        if not neo4j_uri or not neo4j_user:
            return _fail("No credentials in .env or environment variables")

        user_hash = _derive_user_hash(neo4j_uri, neo4j_user)

        try:
            resp = httpx.Client(timeout=3.0).get(
                f"{_API_BASE_URL}/api/session/{user_hash}"
            )
        except (httpx.ConnectError, httpx.TimeoutException):
            return _fail("Could not reach API to restore session")

        if resp.status_code != 200:
            return _fail(f"API returned status {resp.status_code}")

        data = resp.json()
        if not data.get("found") or data.get("record") is None:
            return _fail("No saved session found")

        record = data["record"]

        state.restore_session(
            run_id=record["run_id"],
            timestamp_seed=record["timestamp_seed"],
            phase=record["phase"],
            schema_version=record.get("schema_version"),
            neo4j_uri=record["neo4j_uri"],
            neo4j_user=record["neo4j_user"],
            neo4j_password=neo4j_password,
            openrouter_api_key=openrouter_api_key,
            reentry_source=record.get("reentry_source"),
        )

        # NOTE: We intentionally do NOT call _notify_api_reload() here.
        # Session restore reads credentials from os.environ (injected by
        # docker-compose env_file at container start), which may be stale.
        # Overwriting Redis with stale values corrupts credentials for the
        # worker.  The API and worker sync from Redis independently; the
        # authoritative credentials are those the user entered in Phase 0.

        st.session_state[_K_RESTORE_RESULT] = {
            "attempted": True,
            "success": True,
            "reason": "",
        }
        return True
    except Exception as exc:
        return _fail(f"Unexpected error: {exc}")


def _reconcile_phase(state: StateManager) -> None:
    """Advance the session phase if the backend is ahead of the saved state.

    After a session restore, the saved phase may be stale — e.g., extraction
    completed while the user was away but the phase is still EXTRACTION.
    This function queries backend endpoints to detect completed phases and
    advances the state accordingly.

    Fails silently on any error — falls back to the saved phase.
    """
    # Suppress reconciliation during phase re-entry — the user intentionally
    # navigated backward (e.g. CURATION → INGESTION to add more documents).
    # Without this guard, reconciliation would auto-advance back to CURATION.
    if state.reentry_source is not None:
        return

    try:
        import httpx

        client = httpx.Client(timeout=2.0)
        run_id = state.run_id
        if not run_id:
            return

        # If below INGESTION, check if schema is locked.
        if state.phase.value < Phase.INGESTION.value:
            try:
                resp = client.get(f"{_API_BASE_URL}/api/schema/{run_id}")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("locked"):
                        vh = data.get("schema_version", {}).get("version_hash")
                        if vh:
                            state.set_schema_version(vh)
                        state.advance_phase(Phase.INGESTION)
            except Exception:
                pass

        # If below EXTRACTION, check if documents exist.
        if state.phase.value < Phase.EXTRACTION.value:
            try:
                resp = client.get(f"{_API_BASE_URL}/api/documents/{run_id}")
                if resp.status_code == 200:
                    data = resp.json()
                    docs = data.get("documents", [])
                    if docs:
                        state.advance_phase(Phase.EXTRACTION)
            except Exception:
                pass

        # If below CURATION, check if extraction is complete.
        if state.phase.value < Phase.CURATION.value:
            try:
                resp = client.get(
                    f"{_API_BASE_URL}/api/extraction/status/{run_id}"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    total = data.get("total_chunks", 0)
                    pending = data.get("pending", 0)
                    completed = data.get("completed", 0)
                    failed = data.get("failed", 0)
                    if total > 0 and pending == 0 and completed > 0 and failed == 0:
                        state.advance_phase(Phase.CURATION)
            except Exception:
                pass

    except Exception:
        pass  # Fail silently — fall back to saved phase.


def _save_session(state: StateManager) -> None:
    """Persist current session state to Redis via the API (fire-and-forget).

    Only saves when the session state has actually changed since the last
    save (dirty-check via snapshot hash comparison).  Called on every
    Streamlit rerun when a run_id exists.
    """
    if not state.run_id or not state.neo4j_uri or not state.neo4j_user:
        return

    try:
        import httpx

        # Build a snapshot hash to detect changes.
        reentry_val = state.reentry_source.value if state.reentry_source is not None else ""
        snapshot = (
            f"{state.run_id}|{state.phase.value}|"
            f"{state.schema_version or ''}|{state.neo4j_uri}|{state.neo4j_user}|"
            f"{reentry_val}"
        )
        snapshot_hash = hashlib.sha256(snapshot.encode()).hexdigest()[:16]

        if state.last_saved_hash == snapshot_hash:
            return  # No change since last save.

        user_hash = _derive_user_hash(state.neo4j_uri, state.neo4j_user)

        payload = {
            "user_hash": user_hash,
            "record": {
                "run_id": state.run_id,
                "timestamp_seed": state.timestamp_seed,
                "phase": state.phase.value,
                "schema_version": state.schema_version,
                "neo4j_uri": state.neo4j_uri,
                "neo4j_user": state.neo4j_user,
                "reentry_source": (
                    state.reentry_source.value
                    if state.reentry_source is not None
                    else None
                ),
            },
        }

        httpx.Client(timeout=2.0).post(
            f"{_API_BASE_URL}/api/session/save",
            json=payload,
        )

        state.set_last_saved_hash(snapshot_hash)
        st.session_state.pop(_K_SAVE_DEGRADED, None)
    except Exception:
        st.session_state[_K_SAVE_DEGRADED] = True


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def _render_sidebar(state: StateManager) -> str | None:
    """Render the persistent run-state sidebar and return the selected view.

    Shows phase progression indicators, a compact run summary, and a view
    selector for utility pages. Visible on every page rerun.

    Returns:
        The selected utility view name, or None for the default phase view.
    """
    view_override: str | None = None
    current_phase = state.phase

    with st.sidebar:
        # 1. Logo
        st.image(_load_logo(str(_LOGO_DARK)), width=160)
        # 2. Caption
        st.caption("AI-Powered Graph Curation")

        # 3. New Session button
        if state.run_id:
            if st.button("New Session", type="secondary", key="_nav_new_session"):
                # Best-effort cleanup of persisted session in Redis.
                try:
                    import httpx

                    user_hash = _derive_user_hash(
                        state.neo4j_uri or "", state.neo4j_user or ""
                    )
                    httpx.Client(timeout=2.0).delete(
                        f"{_API_BASE_URL}/api/session/{user_hash}"
                    )
                except Exception:
                    pass

                state.reset()
                st.session_state.pop(_K_RESTORE_ATTEMPTED, None)
                st.rerun()

        # 4. Divider
        st.divider()

        # 5. Activity feed
        if state.run_id:
            render_activity_feed(state.run_id)
            # 6. Divider
            st.divider()

        # 7. View selector
        if state.run_id:
            view_options = ["Current Phase", "Dashboard"]

            # During re-entry, offer navigation back to the source phase
            # and preserve Graph Explorer if the user came from Curation.
            reentry_src = state.reentry_source
            effective_max_phase = current_phase
            if reentry_src is not None:
                effective_max_phase = max(
                    current_phase, reentry_src, key=lambda p: p.value
                )
                _reentry_labels = {
                    Phase.CURATION: "Curation",
                    Phase.EXTRACTION: "Extraction",
                }
                label = _reentry_labels.get(reentry_src)
                if label:
                    view_options.append(label)

            if effective_max_phase.value >= Phase.CURATION.value:
                view_options.append("Graph Explorer")

            selected_view = st.radio(
                "View",
                options=view_options,
                index=0,
                key="_nav_view_radio",
                label_visibility="collapsed",
            )
            if selected_view != "Current Phase":
                view_override = selected_view

            # 8. Divider
            st.divider()

        # 9. Phase progression indicators.
        st.subheader("Phases")
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

        # 10. Divider
        st.divider()

        # 11. Run State section
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

    return view_override


# ---------------------------------------------------------------------------
# Phase 0: Session Initialization
# ---------------------------------------------------------------------------


def _render_phase_init(state: StateManager) -> None:
    """Phase 0: collect credentials and initialize the session.

    Uses st.form so all fields are submitted atomically. No API calls are
    made here — credentials are stored in StateManager for use by later
    phases when constructing API requests (SKILL-B).
    """
    col_logo, col_title = st.columns([1, 4])
    with col_logo:
        st.image(_load_logo(str(_LOGO_DARK)), width=120)
    with col_title:
        st.title("neo4all")
        st.caption("Phase 0 — Session Initialization")
    st.write(
        "Enter your connection details to start a new extraction and curation session. "
        "Credentials are saved to the local `.env` file so the API backend can use them."
    )

    # Surface restore diagnostics if a restore was attempted but failed.
    restore_result = st.session_state.get(_K_RESTORE_RESULT)
    if restore_result and restore_result.get("attempted") and not restore_result.get("success"):
        st.warning(
            f"Could not restore previous session: {restore_result.get('reason', 'unknown error')}. "
            "Please enter your credentials to start a new session."
        )

    # Pre-fill form fields from .env when available (dev convenience).
    env_creds = _read_env_credentials()

    with st.form("phase_0_init_form"):
        st.subheader("Neo4j Aura")
        neo4j_uri = st.text_input(
            "URI",
            value=state.neo4j_uri or env_creds["neo4j_uri"],
            placeholder="neo4j+s://xxxxxxxx.databases.neo4j.io",
            help="Your Neo4j Aura connection URI (starts with neo4j+s://).",
        )
        neo4j_user = st.text_input(
            "Username",
            value=state.neo4j_user or env_creds["neo4j_user"],
            placeholder="neo4j",
            help="Neo4j database username.",
        )
        neo4j_password = st.text_input(
            "Password",
            type="password",
            value=env_creds["neo4j_password"],
            placeholder="Enter password",
            help="Neo4j database password. Saved to `.env`; never logged.",
        )

        st.subheader("LLM Gateway")
        openrouter_api_key = st.text_input(
            "OpenRouter API Key",
            type="password",
            value=env_creds["openrouter_api_key"],
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
            # Redis is the single source of truth for runtime credentials;
            # no need to inject into os.environ.
            if _notify_api_reload(
                neo4j_uri=neo4j_uri.strip(),
                neo4j_user=neo4j_user.strip(),
                neo4j_password=neo4j_password,
                openrouter_api_key=openrouter_api_key,
            ):
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


def _route_phase(state: StateManager) -> None:
    """Route to the page for the current phase.

    Simple if/elif dispatch — deterministic, no widget state to manage.
    Each phase module's main() handles its own content and phase guards.
    """
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


def _route_utility_view(view: str) -> None:
    """Route to a utility page selected from the sidebar."""
    if view == "Dashboard":
        from ui.pages import dashboard as dashboard_page
        dashboard_page.main()
    elif view == "Graph Explorer":
        from ui.pages import graph_explorer as graph_explorer_page
        graph_explorer_page.main()
    elif view == "Curation":
        from ui.pages import curation as curation_page
        curation_page.main()
    elif view == "Extraction":
        from ui.pages import extraction as extraction_page
        extraction_page.main()
    else:
        st.error(f"Unknown view: {view!r}")


@st.cache_resource
def _load_logo(path: str) -> Image.Image:
    """Load a logo image once and cache across reruns."""
    return Image.open(path)


def main() -> None:
    """Application entry point, called by Streamlit on every script rerun."""
    st.set_page_config(
        page_title="neo4all",
        page_icon=_load_logo(str(_LOGO_LIGHT)),
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Hide the default Streamlit "Deploy" button from the toolbar.
    st.markdown(
        '<style>[data-testid="stAppDeployButton"] {display: none;}</style>',
        unsafe_allow_html=True,
    )

    # StateManager.get() seeds defaults on first load and returns a live view.
    state = StateManager.get()

    # --- Session restore (runs once: phase==INIT and run_id==None) ----------
    # On a completely fresh bootstrap, attempt to recover the previous session
    # from Redis + .env.  After restore both conditions become False, so this
    # block is skipped on all subsequent reruns (no infinite loop).
    if state.phase == Phase.INIT and state.run_id is None:
        if not st.session_state.get(_K_RESTORE_ATTEMPTED):
            st.session_state[_K_RESTORE_ATTEMPTED] = True
            if _try_restore_session(state):
                _reconcile_phase(state)   # advance if backend is ahead
                _save_session(state)      # persist reconciled phase to Redis
                st.rerun()

    # --- Session save (fire-and-forget, dirty-check gated) -----------------
    _save_session(state)

    if st.session_state.get(_K_SAVE_DEGRADED):
        st.toast("Session persistence degraded — could not save to Redis.", icon="⚠️")

    # Render sidebar (returns utility view name or None for phase view).
    view_override = _render_sidebar(state)

    # Route to the selected view.
    if view_override:
        _route_utility_view(view_override)
    else:
        _route_phase(state)


if __name__ == "__main__":
    main()
