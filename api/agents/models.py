"""
api/agents/models.py — Agent output contracts (SPEC-07 S-07.5).

Typed Pydantic models for all agent outputs in the curation pipeline:
  - EvidenceReport    (Agent-A output)
  - RetrievalResult   (Agent-B output)
  - AgentProposalOutput (Agent-P output)

All models are frozen (immutable once constructed) and validated on
construction.  Malformed agent responses MUST fail closed — callers
catch ValidationError and emit a fallback (CLAUDE.md §4.4).

Deterministic identity: no uuid4() anywhere.  All IDs are either
inherited from upstream artifacts (candidate_id, chunk_id) or derived
via deterministic hashing.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EvidenceClassification(StrEnum):
    """How a piece of evidence relates to the candidate under review.

    supporting:     Directly confirms the candidate's assessment.
    corroborating:  Independently consistent — strengthens without direct proof.
    conflicting:    Contradicts or undermines the candidate's assessment.
    """

    supporting = "supporting"
    corroborating = "corroborating"
    conflicting = "conflicting"


# ---------------------------------------------------------------------------
# Evidence models (Agent-A output)
# ---------------------------------------------------------------------------


class PriorProposalSummary(BaseModel):
    """Summary of a prior proposal for the same candidate, used for escalation.

    When a canonicalize proposal was previously executed for a candidate and the
    candidate reappears, the structural recommendation escalates to merge.

    Attributes:
        proposal_class: The class of the prior proposal (e.g. "canonicalize").
        confidence:     Confidence score of the prior proposal.
        outcome:        Execution outcome ("applied", "rejected", etc.) or "pending".
    """

    model_config = ConfigDict(frozen=True)

    proposal_class: str
    confidence: float = Field(ge=0.0, le=1.0)
    outcome: str


class EvidenceItem(BaseModel):
    """A single piece of evidence linked to a candidate.

    Attributes:
        chunk_id:        64-char SHA-256 hex digest of the source chunk.
        source_doc:      Identifier of the parent document (doc_id).
        page_locator:    Human-readable page/section locator within the doc.
        classification:  Relationship of this evidence to the candidate.
        relevance_score: 0.0–1.0 — how relevant this evidence is to the
                         candidate.  Assigned by Agent-A.
    """

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    source_doc: str
    page_locator: str = ""
    classification: EvidenceClassification
    relevance_score: float = Field(ge=0.0, le=1.0)

    @field_validator("chunk_id")
    @classmethod
    def _chunk_id_hex64(cls, v: str) -> str:
        if len(v) != 64:
            raise ValueError(
                f"chunk_id must be a 64-character SHA-256 hex digest, got {len(v)} chars"
            )
        return v

    @field_validator("source_doc")
    @classmethod
    def _non_empty_source_doc(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source_doc must be a non-empty string")
        return v


class StructuralRecommendation(BaseModel):
    """Deterministic recommendation based on collision_context metrics.

    Computed from governed detectors (not LLM).  This is the primary
    decision input for Agent-P when no textual evidence is available.

    Attributes:
        suggested_action:  One of the ProposalClass values.
        confidence:        0.0–1.0 — deterministic confidence from metrics.
        reasoning:         Human-readable explanation of the recommendation.
    """

    model_config = ConfigDict(frozen=True)

    suggested_action: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


class EvidenceReport(BaseModel):
    """Typed output of Agent-A (Evidence Assembly).

    Contains all evidence items gathered for a candidate, a computed
    sufficiency score, and a boolean flag indicating whether the evidence
    is sufficient for Agent-P to compose a proposal.

    When ``sufficient`` is False the Orchestrator may trigger Agent-B
    for additional retrieval rounds.

    Attributes:
        candidate_id:     64-char SHA-256 of the candidate being reviewed.
        items:            Evidence items gathered from Qdrant chunks.
        sufficiency_score: 0.0–1.0 — overall evidence sufficiency.
        sufficient:       True if the evidence meets the threshold for
                          proposal composition.
        structural_recommendation: Deterministic pre-assessment from
                          collision_context metrics.  Primary input for
                          Agent-P when textual evidence is sparse.
        run_id:           Governed run identifier.
        schema_version:   Schema version_hash at time of evidence assembly.
    """

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    items: tuple[EvidenceItem, ...] = Field(default_factory=tuple)
    sufficiency_score: float = Field(ge=0.0, le=1.0)
    sufficient: bool
    structural_recommendation: StructuralRecommendation | None = Field(
        default=None,
        description=(
            "Deterministic recommendation from collision_context metrics. "
            "Primary decision input for Agent-P when textual evidence is "
            "sparse or absent."
        ),
    )
    retrieval_exhausted: bool = Field(
        default=False,
        description=(
            "True when retrieval augmentation ran but found no chunks "
            "beyond those already attached to the candidate. Signals "
            "that the pre-attached evidence is the complete picture, "
            "not that evidence is missing."
        ),
    )
    run_id: str
    schema_version: str

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id_hex64(cls, v: str) -> str:
        if len(v) != 64:
            raise ValueError(
                f"candidate_id must be a 64-character SHA-256 hex digest, got {len(v)} chars"
            )
        return v

    @field_validator("run_id", "schema_version")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


# ---------------------------------------------------------------------------
# Retrieval models (Agent-B output)
# ---------------------------------------------------------------------------


class RetrievalResult(BaseModel):
    """Typed output of Agent-B (Retrieval Augmentation).

    Records the retrieval query, chunks found, rounds consumed, and
    remaining budget.  The Orchestrator uses ``budget_remaining`` and
    ``rounds_used`` to enforce loop guards.

    Attributes:
        query:            The semantic query issued to Qdrant.
        chunks_retrieved: Chunk IDs returned by the retrieval round.
        rounds_used:      Number of retrieval rounds completed so far
                          (including this one).
        budget_remaining: Remaining token budget after this round.
    """

    model_config = ConfigDict(frozen=True)

    query: str
    chunks_retrieved: tuple[str, ...] = Field(default_factory=tuple)
    rounds_used: int = Field(ge=0)
    budget_remaining: int = Field(ge=0)

    @field_validator("query")
    @classmethod
    def _non_empty_query(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must be a non-empty string")
        return v


# ---------------------------------------------------------------------------
# Proposal models (Agent-P output)
# ---------------------------------------------------------------------------


class AgentProposalOutput(BaseModel):
    """Typed output of Agent-P (Proposal Composer).

    Contains all fields needed by the curation router to construct a
    governed ProposalPacket.  Agent-P selects the proposal class, cites
    rule IDs and evidence chunk IDs, writes a rationale, and assigns a
    confidence score.

    Never contains: Cypher, executable instructions, or diff steps.

    Attributes:
        candidate_id:     64-char SHA-256 of the candidate.
        proposal_class:   Curation action type (reuses ProposalClass enum).
        rule_ids:         Schema/governance rule identifiers motivating
                          this proposal.
        evidence_ids:     Chunk IDs cited as supporting evidence.
        rationale:        Human-readable explanation of the proposed action.
        confidence_score: 0.0–1.0 — Agent-P's confidence in this proposal.
    """

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    proposal_class: str
    rule_ids: tuple[str, ...] = Field(default_factory=tuple)
    evidence_ids: tuple[str, ...] = Field(default_factory=tuple)
    rationale: str
    confidence_score: float = Field(ge=0.0, le=1.0)

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id_hex64(cls, v: str) -> str:
        if len(v) != 64:
            raise ValueError(
                f"candidate_id must be a 64-character SHA-256 hex digest, got {len(v)} chars"
            )
        return v

    @field_validator("proposal_class")
    @classmethod
    def _valid_proposal_class(cls, v: str) -> str:
        from api.proposals.models import ProposalClass

        allowed = {e.value for e in ProposalClass}
        if v not in allowed:
            raise ValueError(
                f"proposal_class must be one of {sorted(allowed)}, got {v!r}"
            )
        return v

    @field_validator("rationale")
    @classmethod
    def _non_empty_rationale(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("rationale must be a non-empty, non-whitespace string")
        return v
