"""
api/proposals/models.py — Proposal Packet models (SPEC-06 S-06.1).

Implements the full Proposal Packet per CLAUDE.md §6.  Proposal Packets are
composed by Agent-P (or a human operator) and carry all fields required by the
Diff Builder and Approval Gate.  They never contain Cypher, executable
instructions, or runnable mutations.

Deterministic identity contract (CLAUDE.md §5):
    proposal_id = SHA-256(canonical_json({
        "run_id": ...,
        "candidate_id": ...,
        "proposal_class": ...,
    }))

proposal_id is a @computed_field — never accepted as constructor input, always
re-derived from the three linkage fields.  Because the model is frozen the
computed value is stable for the model's lifetime.

State machine:
    pending → approved | rejected | deferred

Only a Proposal in "pending" state may transition.  The transition is performed
by ProposalService (api/proposals/service.py) — models are immutable (frozen)
so transitions return new model instances.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


# ---------------------------------------------------------------------------
# Vocabulary enums
# ---------------------------------------------------------------------------


class ProposalClass(StrEnum):
    """The type of curation action proposed.

    canonicalize: Standardise property values to a canonical form (e.g. name
                  casing, whitespace normalisation, URI formatting).
    normalize:    Apply schema-level normalisation rules (units, date formats,
                  enumeration members).
    rename:       Change the primary identifier / display label of a node.
    merge:        Collapse two or more nodes/edges into a single entity.
                  HIGH RISK — requires two-phase approval.
    delete:       Remove a node or edge from the graph.
                  HIGH RISK — requires two-phase approval.
    defer:        Acknowledge the candidate but take no action at this time.
    """

    canonicalize = "canonicalize"
    normalize = "normalize"
    rename = "rename"
    merge = "merge"
    delete = "delete"
    defer = "defer"


# High-risk classes require two-phase approval (SPEC-06 S-06.3).
HIGH_RISK_CLASSES: frozenset[ProposalClass] = frozenset(
    {ProposalClass.merge, ProposalClass.delete}
)


class ProposalState(StrEnum):
    """Lifecycle state of a Proposal Packet.

    pending:  Initial state.  Awaiting human approval.
    approved: Approved by an authorised human actor.  Diff may now be applied.
    rejected: Explicitly rejected.  No further transitions permitted.
    deferred: Acknowledged but set aside.  Can be re-opened (pending) later.
    """

    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    deferred = "deferred"


# Valid state transitions: {from_state: {allowed_to_states}}
_VALID_TRANSITIONS: dict[ProposalState, frozenset[ProposalState]] = {
    ProposalState.pending: frozenset(
        {ProposalState.approved, ProposalState.rejected, ProposalState.deferred}
    ),
    ProposalState.approved: frozenset(),   # terminal
    ProposalState.rejected: frozenset(),   # terminal
    ProposalState.deferred: frozenset({ProposalState.pending}),  # re-openable
}


def allowed_transitions(state: ProposalState) -> frozenset[ProposalState]:
    """Return the set of states reachable from *state*."""
    return _VALID_TRANSITIONS[state]


# ---------------------------------------------------------------------------
# Target reference — identifies a graph element affected by the proposal
# ---------------------------------------------------------------------------


class ElementRef(BaseModel):
    """A typed reference to a graph element targeted by this proposal.

    element_type:  "node" or "relationship".
    dedupe_key:    The deterministic identity key for the element
                   (NodeType, primary_property, schema_version) tuple stringified,
                   or the equivalent for relationships.
    label:         Human-readable label for display in the curation UI.
    properties:    Snapshot of current property values (evidence, not truth).
    """

    model_config = ConfigDict(frozen=True)

    element_type: str   # "node" | "relationship"
    dedupe_key: str
    label: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("element_type")
    @classmethod
    def _valid_element_type(cls, v: str) -> str:
        allowed = {"node", "relationship"}
        if v not in allowed:
            raise ValueError(f"element_type must be one of {allowed!r}, got {v!r}")
        return v

    @field_validator("dedupe_key")
    @classmethod
    def _non_empty_dedupe_key(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("dedupe_key must be a non-empty, non-whitespace string")
        return v


# ---------------------------------------------------------------------------
# ProposalPacket — the core governed artifact
# ---------------------------------------------------------------------------


class ProposalPacket(BaseModel):
    """Immutable Proposal Packet — the unit of governed mutation intent.

    Composed by Agent-P or a human operator.  Carries all information needed by
    the Diff Builder to produce a deterministic diff and by the Approval Gate to
    validate and record a human decision.

    Linkage fields (also form the proposal_id identity):
        run_id:          Governed run this proposal belongs to.
        candidate_id:    Candidate that motivated this proposal (64-char SHA-256).
        proposal_class:  The category of curation action being proposed.

    Intent / Evidence fields:
        evidence_chunk_ids:   Chunk IDs from Qdrant that support the proposal.
        evidence_doc_ids:     Source document IDs for provenance.
        targets:              Graph elements the diff will act upon.

    Governance fields:
        rule_ids:         Identifiers of schema rules that motivate this proposal.
        rationale:        Human-readable explanation (required, non-empty).
        confidence_score: 0.0–1.0 confidence from Agent-P, or 1.0 for human.

    State:
        state:            Lifecycle state (pending initially).

    Schema linkage:
        schema_version:   Schema version_hash at the time of proposal creation.

    Computed:
        proposal_id:      SHA-256 of (run_id, candidate_id, proposal_class).
    """

    model_config = ConfigDict(frozen=True)

    # ---- Linkage ----
    run_id: str
    candidate_id: str
    proposal_class: ProposalClass
    schema_version: str

    # ---- Intent / Evidence ----
    evidence_chunk_ids: tuple[str, ...] = Field(default_factory=tuple)
    evidence_doc_ids: tuple[str, ...] = Field(default_factory=tuple)
    targets: tuple[ElementRef, ...] = Field(default_factory=tuple)

    # ---- Governance ----
    rule_ids: tuple[str, ...] = Field(default_factory=tuple)
    rationale: str
    confidence_score: float = Field(ge=0.0, le=1.0, default=1.0)

    # ---- State ----
    state: ProposalState = ProposalState.pending

    # ---- Validators ----

    @field_validator("run_id", "candidate_id", "schema_version")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v

    @field_validator("rationale")
    @classmethod
    def _non_empty_rationale(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("rationale must be a non-empty, non-whitespace string")
        return v

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id_length(cls, v: str) -> str:
        if len(v) != 64:
            raise ValueError(
                f"candidate_id must be a 64-character SHA-256 hex digest, got {len(v)} chars"
            )
        return v

    # ---- Computed identity ----

    @computed_field  # type: ignore[prop-decorator]
    @property
    def proposal_id(self) -> str:
        """Deterministic SHA-256 hex digest identifying this proposal.

        Identity inputs (per CLAUDE.md §5):
            run_id, candidate_id, proposal_class

        These three fields uniquely identify the intent: *which run*, *which
        candidate*, and *what action*.  Evidence, governance fields, and state
        are deliberately excluded — they do not alter the identity of the
        proposed action.

        Canonical JSON (sort_keys, compact, ensure_ascii) for byte-stable
        serialisation.
        """
        identity: dict[str, str] = {
            "run_id": self.run_id,
            "candidate_id": self.candidate_id,
            "proposal_class": str(self.proposal_class),
        }
        serialized = json.dumps(
            identity,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    # ---- Transition helper ----

    def is_high_risk(self) -> bool:
        """Return True if this proposal class requires two-phase approval."""
        return self.proposal_class in HIGH_RISK_CLASSES

    def can_transition_to(self, target: ProposalState) -> bool:
        """Return True if a transition from self.state → target is valid."""
        return target in allowed_transitions(self.state)
