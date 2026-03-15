"""
ui/state.py — StateManager: the sole interface to st.session_state.

CLAUDE.md §8 mandates that NO code in the ui/ layer may read or write
st.session_state directly. All session state access flows through the
StateManager class defined here.

Usage (the only correct pattern):
    state = StateManager.get()          # initialize defaults + return live view
    phase = state.phase                 # read
    state.advance_phase(Phase.SCHEMA)   # mutate with ordering enforcement

Forbidden everywhere except this file:
    st.session_state["current_phase"] = "extraction"   # BANNED

Phase ordering
--------------
Phases are strictly ordered integers. advance_phase() accepts only the
immediate next phase (current.value + 1). Any attempt to skip a phase,
go backwards, or advance from the final phase raises ValueError.

Panel mode tracking
-------------------
Named UI panels can be in "read" or "write" mode. Panels switch between
modes as the user navigates the workflow. StateManager tracks these so
that any component can query a panel's current mode without coupling to
sibling components.

Run ID derivation
-----------------
run_id is deterministic: sha256(neo4j_user + timestamp_seed).
timestamp_seed is captured once at session initialization and stored.
Both are used identically to the API-side derive_run_id() in
api/models/run.py — imported directly to avoid duplication (CLAUDE.md §8
explicitly permits ui/ to import Phase from api/models/run.py, and
api/models/ is not in the banned import list per SKILL-B R-B3).
"""

from datetime import UTC, datetime
from typing import Any

import streamlit as st

from api.models.run import Phase, derive_run_id

# ---------------------------------------------------------------------------
# Session state key constants
# All keys are prefixed with _SM_ to prevent collisions with widget keys.
# Only StateManager methods may read or write these keys.
# ---------------------------------------------------------------------------
_K_PHASE = "_sm_phase"
_K_RUN_ID = "_sm_run_id"
_K_TIMESTAMP_SEED = "_sm_timestamp_seed"
_K_SCHEMA_VERSION = "_sm_schema_version"
_K_NEO4J_URI = "_sm_neo4j_uri"
_K_NEO4J_USER = "_sm_neo4j_user"
_K_NEO4J_PASSWORD = "_sm_neo4j_password"
_K_OPENROUTER_API_KEY = "_sm_openrouter_api_key"
_K_PANEL_MODES = "_sm_panel_modes"
_K_PHASE_START_TIMES = "_sm_phase_start_times"
# Schema proposal — intermediate state between propose and approve (SPEC-02).
# Cleared on session reset; stored in StateManager rather than directly in
# st.session_state so that all access follows the CLAUDE.md §8 contract.
_K_PROPOSED_NODES = "_sm_proposed_nodes"      # list[dict] | None
_K_PROPOSED_EDGES = "_sm_proposed_edges"      # list[dict] | None
_K_PROPOSAL_VERSION = "_sm_proposal_version"  # int; incremented on each propose
# Session persistence — dirty-check hash to avoid redundant API saves.
_K_LAST_SAVED_HASH = "_sm_last_saved_hash"    # str | None
# Dismissed proposals — hidden from the UI queue but preserved for governance.
_K_DISMISSED_PROPOSALS = "_sm_dismissed_proposals"  # set[str]
# Pending confirmations — auto-passed tokens for two-click high-risk approval.
_K_PENDING_CONFIRMATIONS = "_sm_pending_confirmations"  # dict[str, str]
# Submitted candidates — tracks which candidates have had proposals submitted this session.
_K_SUBMITTED_CANDIDATES = "_sm_submitted_candidates"  # set[str]
# Phase re-entry — records which phase the user came from when navigating backward.
_K_REENTRY_SOURCE = "_sm_reentry_source"  # Phase | None

# Allowed panel mode values.
PANEL_READ = "read"
PANEL_WRITE = "write"


class StateManager:
    """Sole interface to st.session_state for the neo4all Streamlit app.

    Obtain an instance with StateManager.get(), which seeds defaults on the
    first call of each Streamlit session and returns a live view object.
    All properties read from st.session_state on access (no stale snapshots).
    All mutation methods write to st.session_state immediately.

    Direct st.session_state access outside this class is banned.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def get(cls) -> "StateManager":
        """Return a live StateManager view of the current session state.

        Idempotent: seeds default values into st.session_state on the first
        call within a Streamlit session, then returns a fresh instance on
        every call.
        """
        cls._bootstrap()
        return cls()

    @classmethod
    def _bootstrap(cls) -> None:
        """Seed session state with default values on first load.

        Only sets keys that are not already present so that Streamlit reruns
        (caused by widget interactions) do not overwrite live state.
        """
        init_time = datetime.now(UTC).isoformat()
        defaults: dict[str, Any] = {
            _K_PHASE: Phase.INIT,
            _K_RUN_ID: None,
            _K_TIMESTAMP_SEED: None,
            _K_SCHEMA_VERSION: None,
            _K_NEO4J_URI: None,
            _K_NEO4J_USER: None,
            _K_NEO4J_PASSWORD: None,
            _K_OPENROUTER_API_KEY: None,
            _K_PANEL_MODES: {},
            _K_PHASE_START_TIMES: {Phase.INIT.value: init_time},
            _K_PROPOSED_NODES: None,
            _K_PROPOSED_EDGES: None,
            _K_PROPOSAL_VERSION: 0,
            _K_LAST_SAVED_HASH: None,
            _K_DISMISSED_PROPOSALS: set(),
            _K_PENDING_CONFIRMATIONS: {},
            _K_SUBMITTED_CANDIDATES: set(),
            _K_REENTRY_SOURCE: None,
        }
        for key, default in defaults.items():
            if key not in st.session_state:
                st.session_state[key] = default

    # ------------------------------------------------------------------
    # Read properties — live access to st.session_state
    # ------------------------------------------------------------------

    @property
    def phase(self) -> Phase:
        """Current application phase."""
        return st.session_state[_K_PHASE]

    @property
    def run_id(self) -> str | None:
        """Deterministic run ID, set by initialize_session()."""
        return st.session_state[_K_RUN_ID]

    @property
    def timestamp_seed(self) -> str | None:
        """ISO timestamp captured at session initialization."""
        return st.session_state[_K_TIMESTAMP_SEED]

    @property
    def schema_version(self) -> str | None:
        """Domain schema version string, set when schema is locked in Phase 1."""
        return st.session_state[_K_SCHEMA_VERSION]

    @property
    def neo4j_uri(self) -> str | None:
        """Neo4j Aura URI entered during Phase 0."""
        return st.session_state[_K_NEO4J_URI]

    @property
    def neo4j_user(self) -> str | None:
        """Neo4j user entered during Phase 0."""
        return st.session_state[_K_NEO4J_USER]

    @property
    def neo4j_password(self) -> str | None:
        """Neo4j password entered during Phase 0. Never logged."""
        return st.session_state[_K_NEO4J_PASSWORD]

    @property
    def openrouter_api_key(self) -> str | None:
        """OpenRouter API key entered during Phase 0. Never logged."""
        return st.session_state[_K_OPENROUTER_API_KEY]

    @property
    def phase_start_times(self) -> dict[int, str]:
        """Map of Phase.value → ISO 8601 UTC start time for each entered phase."""
        return st.session_state[_K_PHASE_START_TIMES]

    @property
    def proposed_nodes(self) -> list[Any] | None:
        """LLM-proposed node type dicts from the last propose call, or None."""
        return st.session_state[_K_PROPOSED_NODES]  # type: ignore[return-value]

    @property
    def proposed_edges(self) -> list[Any] | None:
        """LLM-proposed edge type dicts from the last propose call, or None."""
        return st.session_state[_K_PROPOSED_EDGES]  # type: ignore[return-value]

    @property
    def proposal_version(self) -> int:
        """Monotonically increasing counter; incremented on each propose call.

        Used as a widget key suffix so that each new proposal gets a fresh
        data_editor widget (prevents stale edits carrying over to a new proposal).
        """
        return st.session_state[_K_PROPOSAL_VERSION]  # type: ignore[return-value]

    @property
    def last_saved_hash(self) -> str | None:
        """Hash of the last successfully saved session snapshot, or None."""
        return st.session_state[_K_LAST_SAVED_HASH]

    @property
    def dismissed_proposals(self) -> set[str]:
        """Set of proposal IDs dismissed from the UI queue."""
        return st.session_state[_K_DISMISSED_PROPOSALS]

    @property
    def submitted_candidates(self) -> set[str]:
        """Set of candidate IDs that have had proposals submitted this session."""
        return st.session_state[_K_SUBMITTED_CANDIDATES]

    @property
    def reentry_source(self) -> Phase | None:
        """Phase the user navigated back from, or None if not in re-entry mode."""
        return st.session_state[_K_REENTRY_SOURCE]

    # ------------------------------------------------------------------
    # Panel mode tracking
    # ------------------------------------------------------------------

    def get_panel_mode(self, panel: str, *, default: str = PANEL_WRITE) -> str:
        """Return the current display mode for a named panel.

        Args:
            panel:   Panel identifier string (e.g. "schema_editor").
            default: Mode to return when the panel has not been set.
                     Defaults to PANEL_WRITE so panels start in editable mode.

        Returns:
            "read" or "write".
        """
        modes: dict[str, str] = st.session_state[_K_PANEL_MODES]
        return modes.get(panel, default)

    def set_panel_mode(self, panel: str, mode: str) -> None:
        """Set the display mode for a named panel.

        Args:
            panel: Panel identifier string.
            mode:  "read" or "write". Invalid values are rejected.

        Raises:
            ValueError: If mode is not "read" or "write".
        """
        if mode not in (PANEL_READ, PANEL_WRITE):
            raise ValueError(
                f"Invalid panel mode {mode!r}. Must be {PANEL_READ!r} or {PANEL_WRITE!r}."
            )
        modes: dict[str, str] = dict(st.session_state[_K_PANEL_MODES])
        modes[panel] = mode
        st.session_state[_K_PANEL_MODES] = modes

    # ------------------------------------------------------------------
    # Mutation methods
    # ------------------------------------------------------------------

    def advance_phase(self, target: Phase) -> None:
        """Advance to the immediately next phase in strict order.

        Phases must advance one step at a time (0→1, 1→2, …). Skipping
        phases, going backwards, or advancing past CURATION is rejected.

        Args:
            target: The phase to transition into.

        Raises:
            ValueError: If target is not exactly current_phase + 1.
        """
        current = self.phase
        expected_next = current.value + 1

        if target.value != expected_next:
            raise ValueError(
                f"Phase transition rejected: "
                f"{current.name} (phase {current.value}) "
                f"\u2192 {target.name} (phase {target.value}). "
                f"Expected next phase: {expected_next}. "
                f"Phases must advance by exactly one step in order."
            )

        # Record the start time for the new phase.
        timings: dict[int, str] = dict(st.session_state[_K_PHASE_START_TIMES])
        timings[target.value] = datetime.now(UTC).isoformat()
        st.session_state[_K_PHASE_START_TIMES] = timings

        # Commit the phase transition last so timing is always recorded.
        st.session_state[_K_PHASE] = target

    def reenter_phase(self, target: Phase) -> None:
        """Navigate backward to a previous phase for re-entry (e.g. add more documents).

        Only allows specific transitions:
          - CURATION → INGESTION
          - EXTRACTION → INGESTION

        Records the current phase as the re-entry source so the UI can
        suppress reconciliation and adjust button labels.

        Args:
            target: The phase to navigate back to.

        Raises:
            ValueError: If the transition is not an allowed re-entry path.
        """
        current = self.phase
        allowed = {
            (Phase.CURATION, Phase.INGESTION),
            (Phase.EXTRACTION, Phase.INGESTION),
        }
        if (current, target) not in allowed:
            raise ValueError(
                f"Re-entry rejected: {current.name} → {target.name}. "
                f"Allowed re-entry paths: CURATION→INGESTION, EXTRACTION→INGESTION."
            )

        st.session_state[_K_REENTRY_SOURCE] = current

        # Record phase start time for the re-entered phase.
        timings: dict[int, str] = dict(st.session_state[_K_PHASE_START_TIMES])
        timings[target.value] = datetime.now(UTC).isoformat()
        st.session_state[_K_PHASE_START_TIMES] = timings

        st.session_state[_K_PHASE] = target

    def clear_reentry(self) -> None:
        """Clear the re-entry source flag, returning to normal phase flow."""
        st.session_state[_K_REENTRY_SOURCE] = None

    def initialize_session(
        self,
        *,
        neo4j_uri: str,
        neo4j_user: str,
        neo4j_password: str,
        openrouter_api_key: str,
    ) -> None:
        """Store Phase 0 credentials and derive a deterministic run_id.

        Captures the current UTC time as timestamp_seed (once per session).
        run_id is derived as sha256(neo4j_user + timestamp_seed) using the
        same function as the API layer (derive_run_id from api.models.run).

        Credentials are stored in server-side session memory only. They are
        never written to disk or logs (SKILL-D R-D5).

        Args:
            neo4j_uri:          Neo4j Aura connection URI.
            neo4j_user:         Neo4j username (also used as user_namespace).
            neo4j_password:     Neo4j password. Never logged.
            openrouter_api_key: OpenRouter API key. Never logged.
        """
        timestamp_seed = datetime.now(UTC).isoformat()
        run_id = derive_run_id(neo4j_user, timestamp_seed)

        st.session_state[_K_NEO4J_URI] = neo4j_uri
        st.session_state[_K_NEO4J_USER] = neo4j_user
        st.session_state[_K_NEO4J_PASSWORD] = neo4j_password
        st.session_state[_K_OPENROUTER_API_KEY] = openrouter_api_key
        st.session_state[_K_RUN_ID] = run_id
        st.session_state[_K_TIMESTAMP_SEED] = timestamp_seed

    def set_schema_version(self, version: str) -> None:
        """Record the locked schema version string (called during Phase 1).

        Args:
            version: Schema version identifier (e.g. "v1.0").
        """
        st.session_state[_K_SCHEMA_VERSION] = version

    def set_proposed_schema(self, nodes: list[Any], edges: list[Any]) -> None:
        """Store a new schema proposal and increment the proposal version counter.

        Called after a successful POST /api/schema/propose response. The
        version counter increment forces data_editor widgets to re-initialise
        with fresh keys, clearing any edits from a previous proposal.

        Args:
            nodes: List of node type dicts from the API response.
            edges: List of edge type dicts from the API response.
        """
        st.session_state[_K_PROPOSED_NODES] = nodes
        st.session_state[_K_PROPOSED_EDGES] = edges
        st.session_state[_K_PROPOSAL_VERSION] = (
            st.session_state[_K_PROPOSAL_VERSION] + 1
        )

    def clear_proposed_schema(self) -> None:
        """Clear the current proposal so the domain input form is shown again.

        Does NOT reset the proposal_version counter — the next call to
        set_proposed_schema() will increment it again, giving fresh widget keys.
        """
        st.session_state[_K_PROPOSED_NODES] = None
        st.session_state[_K_PROPOSED_EDGES] = None

    def set_last_saved_hash(self, h: str) -> None:
        """Record the hash of the most recently saved session snapshot."""
        st.session_state[_K_LAST_SAVED_HASH] = h

    def dismiss_proposal(self, proposal_id: str) -> None:
        """Hide a proposal from the UI queue (governance/audit trail preserved)."""
        dismissed = set(st.session_state[_K_DISMISSED_PROPOSALS])
        dismissed.add(proposal_id)
        st.session_state[_K_DISMISSED_PROPOSALS] = dismissed

    def dismiss_proposals_batch(self, proposal_ids: set[str]) -> None:
        """Hide multiple proposals from the UI queue at once."""
        dismissed = set(st.session_state[_K_DISMISSED_PROPOSALS])
        dismissed |= proposal_ids
        st.session_state[_K_DISMISSED_PROPOSALS] = dismissed

    def undismiss_proposal(self, proposal_id: str) -> None:
        """Restore a dismissed proposal to the UI queue."""
        dismissed = set(st.session_state[_K_DISMISSED_PROPOSALS])
        dismissed.discard(proposal_id)
        st.session_state[_K_DISMISSED_PROPOSALS] = dismissed

    def clear_dismissed_proposals(self) -> None:
        """Reset the dismissed-proposals set (e.g. after purging all proposals)."""
        st.session_state[_K_DISMISSED_PROPOSALS] = set()

    def set_pending_confirmation(self, proposal_id: str, token: str) -> None:
        """Store a confirmation token for two-click high-risk approval."""
        confirmations = dict(st.session_state[_K_PENDING_CONFIRMATIONS])
        confirmations[proposal_id] = token
        st.session_state[_K_PENDING_CONFIRMATIONS] = confirmations

    def get_pending_confirmation(self, proposal_id: str) -> str | None:
        """Retrieve a stored confirmation token, or None if not pending."""
        return st.session_state[_K_PENDING_CONFIRMATIONS].get(proposal_id)

    def clear_pending_confirmation(self, proposal_id: str) -> None:
        """Remove a stored confirmation token."""
        confirmations = dict(st.session_state[_K_PENDING_CONFIRMATIONS])
        confirmations.pop(proposal_id, None)
        st.session_state[_K_PENDING_CONFIRMATIONS] = confirmations

    def mark_candidate_submitted(self, candidate_id: str) -> None:
        """Record that a proposal has been submitted for a candidate this session."""
        submitted = set(st.session_state[_K_SUBMITTED_CANDIDATES])
        submitted.add(candidate_id)
        st.session_state[_K_SUBMITTED_CANDIDATES] = submitted

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def restore_session(
        self,
        *,
        run_id: str,
        timestamp_seed: str,
        phase: int,
        schema_version: str | None,
        neo4j_uri: str,
        neo4j_user: str,
        neo4j_password: str,
        openrouter_api_key: str,
        reentry_source: int | None = None,
    ) -> None:
        """Restore a previously persisted session, bypassing advance_phase() ordering.

        This is NOT a phase transition — it reconstitutes a known-valid state
        snapshot saved to Redis during a prior run.  The phase value is
        validated as a legal Phase member but the incremental ordering
        constraint is deliberately not enforced.

        Args:
            run_id:             Previously derived run_id.
            timestamp_seed:     ISO timestamp that produced run_id.
            phase:              Phase integer from the persisted record.
            schema_version:     Locked schema version or None.
            neo4j_uri:          Neo4j connection URI.
            neo4j_user:         Neo4j username.
            neo4j_password:     Neo4j password (from .env, not from Redis).
            openrouter_api_key: OpenRouter API key (from .env, not from Redis).
            reentry_source:     Phase integer the user navigated back from, or None.

        Raises:
            ValueError: If phase is not a valid Phase member.
        """
        target_phase = Phase(phase)  # raises ValueError if invalid

        st.session_state[_K_RUN_ID] = run_id
        st.session_state[_K_TIMESTAMP_SEED] = timestamp_seed
        st.session_state[_K_PHASE] = target_phase
        st.session_state[_K_SCHEMA_VERSION] = schema_version
        st.session_state[_K_NEO4J_URI] = neo4j_uri
        st.session_state[_K_NEO4J_USER] = neo4j_user
        st.session_state[_K_NEO4J_PASSWORD] = neo4j_password
        st.session_state[_K_OPENROUTER_API_KEY] = openrouter_api_key
        st.session_state[_K_REENTRY_SOURCE] = (
            Phase(reentry_source) if reentry_source is not None else None
        )

    def reset(self) -> None:
        """Reset all session state to defaults (for "New Session").

        Clears every _sm_* key back to its bootstrap default.  Does NOT
        clear widget keys (_nav_*) — those are managed by Streamlit's
        widget lifecycle.
        """
        init_time = datetime.now(UTC).isoformat()
        st.session_state[_K_PHASE] = Phase.INIT
        st.session_state[_K_RUN_ID] = None
        st.session_state[_K_TIMESTAMP_SEED] = None
        st.session_state[_K_SCHEMA_VERSION] = None
        st.session_state[_K_NEO4J_URI] = None
        st.session_state[_K_NEO4J_USER] = None
        st.session_state[_K_NEO4J_PASSWORD] = None
        st.session_state[_K_OPENROUTER_API_KEY] = None
        st.session_state[_K_PANEL_MODES] = {}
        st.session_state[_K_PHASE_START_TIMES] = {Phase.INIT.value: init_time}
        st.session_state[_K_PROPOSED_NODES] = None
        st.session_state[_K_PROPOSED_EDGES] = None
        st.session_state[_K_PROPOSAL_VERSION] = 0
        st.session_state[_K_LAST_SAVED_HASH] = None
        st.session_state[_K_DISMISSED_PROPOSALS] = set()
        st.session_state[_K_PENDING_CONFIRMATIONS] = {}
        st.session_state[_K_SUBMITTED_CANDIDATES] = set()
        st.session_state[_K_REENTRY_SOURCE] = None
