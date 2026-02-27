"""
api/agents/orchestrator.py -- Non-LLM Orchestrator (SPEC-07 S-07.1).

Receives a batch of candidates, assigns a risk class and tool budget to each,
then returns structured OrchestratorDecision records for the downstream agent
chain (Agent-A -> Agent-B -> Agent-P).

This module contains NO LLM calls.  All logic is deterministic:
  - Risk class derived from candidate_type + implied proposal_class.
  - Tool budget derived from AgentConfig + risk class multipliers.
  - Loop guards enforce max candidates per batch and timeout per candidate.

Determinism requirement (CLAUDE.md $4.3):
  Same candidate list + same AgentConfig => same list of OrchestratorDecision
  (order preserved from input).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from api.config import AgentConfig
from api.models.candidate import Candidate, CandidateType
from api.observability.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum candidates the orchestrator will process in a single batch.
# The orchestrator is non-LLM (O(n) deterministic), so this is a
# defensive upper bound, not a context-window constraint.
# Callers MUST slice inputs before calling orchestrate().
MAX_CANDIDATES_PER_BATCH: int = 500

# Per-candidate wall-clock budget in seconds.  The agent chain must
# complete within this window or the candidate is auto-deferred.
DEFAULT_TIMEOUT_PER_CANDIDATE_S: float = 120.0

# Budget multipliers indexed by risk class.  Applied to the base token
# budgets from AgentConfig to derive the per-candidate budget.
_BUDGET_MULTIPLIERS: dict[str, float] = {
    "low": 0.5,
    "medium": 1.0,
    "high": 1.5,
}


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RiskClass(StrEnum):
    """Risk classification assigned by the Orchestrator per candidate.

    low:    Cosmetic / informational changes (canonicalize, defer).
    medium: Semantic changes with moderate impact (rename, normalize).
    high:   Destructive or structural changes (merge, delete).
    """

    low = "low"
    medium = "medium"
    high = "high"


# ---------------------------------------------------------------------------
# Risk assignment rules
# ---------------------------------------------------------------------------

# Maps (CandidateType) -> default RiskClass.
# merge/delete candidates always escalate to high regardless of type.
_TYPE_RISK: dict[CandidateType, RiskClass] = {
    CandidateType.exact_node_duplicate: RiskClass.medium,
    CandidateType.exact_rel_duplicate: RiskClass.medium,
    CandidateType.probable_duplicate: RiskClass.high,
    CandidateType.canonical_violation: RiskClass.medium,
    CandidateType.structural_anomaly: RiskClass.low,
}

# Proposal classes that force escalation to a specific risk level,
# regardless of candidate type.
_PROPOSAL_CLASS_ESCALATION: dict[str, RiskClass] = {
    "merge": RiskClass.high,
    "delete": RiskClass.high,
    "rename": RiskClass.medium,
    "normalize": RiskClass.medium,
    "canonicalize": RiskClass.low,
    "defer": RiskClass.low,
}


def assign_risk_class(
    candidate: Candidate,
    proposal_class_hint: str | None = None,
) -> RiskClass:
    """Determine the risk class for a candidate.

    The risk class is the *maximum* of:
      1. The default risk derived from candidate_type.
      2. The escalation level implied by proposal_class_hint (if provided).

    Args:
        candidate:            The curation candidate to classify.
        proposal_class_hint:  Optional anticipated proposal class.  When the
                              agent chain has not yet run, this is None and
                              only the candidate type drives risk.

    Returns:
        Deterministic RiskClass value.
    """
    type_risk = _TYPE_RISK.get(candidate.candidate_type, RiskClass.medium)

    if proposal_class_hint is not None:
        escalation = _PROPOSAL_CLASS_ESCALATION.get(
            proposal_class_hint, RiskClass.medium
        )
        # Take the higher of the two risk levels.
        if _risk_ord(escalation) > _risk_ord(type_risk):
            return escalation

    return type_risk


def _risk_ord(risk: RiskClass) -> int:
    """Numeric ordering for RiskClass comparison (low=0, medium=1, high=2)."""
    return {"low": 0, "medium": 1, "high": 2}[risk]


# ---------------------------------------------------------------------------
# Tool budget
# ---------------------------------------------------------------------------


class ToolBudget(BaseModel):
    """Token and cost budgets assigned to the agent chain for one candidate.

    Attributes:
        max_input_tokens_a:   Agent-A prompt token cap.
        max_output_tokens_a:  Agent-A completion token cap.
        max_input_tokens_b:   Agent-B prompt token cap.
        max_output_tokens_b:  Agent-B completion token cap.
        max_input_tokens_p:   Agent-P prompt token cap.
        max_output_tokens_p:  Agent-P completion token cap.
        max_retrieval_rounds: Agent-B loop guard.
        cost_limit:           USD cost cap for the full chain.
        timeout_s:            Wall-clock timeout for the full chain.
    """

    model_config = ConfigDict(frozen=True)

    max_input_tokens_a: int = Field(gt=0)
    max_output_tokens_a: int = Field(gt=0)
    max_input_tokens_b: int = Field(gt=0)
    max_output_tokens_b: int = Field(gt=0)
    max_input_tokens_p: int = Field(gt=0)
    max_output_tokens_p: int = Field(gt=0)
    max_retrieval_rounds: int = Field(ge=1)
    cost_limit: float = Field(gt=0.0)
    timeout_s: float = Field(gt=0.0)


def compute_budget(
    risk: RiskClass,
    config: AgentConfig,
    timeout_s: float = DEFAULT_TIMEOUT_PER_CANDIDATE_S,
) -> ToolBudget:
    """Derive a ToolBudget from AgentConfig and risk class.

    Higher risk candidates receive proportionally larger budgets via the
    multiplier table.  Low-risk candidates get half the base budget; high-risk
    candidates get 1.5x.

    Args:
        risk:      The assigned risk class.
        config:    Global agent configuration (model assignments, base budgets).
        timeout_s: Wall-clock timeout in seconds.

    Returns:
        Frozen ToolBudget.
    """
    mult = _BUDGET_MULTIPLIERS[risk]

    return ToolBudget(
        max_input_tokens_a=int(config.agent_a.max_input_tokens * mult),
        max_output_tokens_a=int(config.agent_a.max_output_tokens * mult),
        max_input_tokens_b=int(config.agent_b.max_input_tokens * mult),
        max_output_tokens_b=int(config.agent_b.max_output_tokens * mult),
        max_input_tokens_p=int(config.agent_p.max_input_tokens * mult),
        max_output_tokens_p=int(config.agent_p.max_output_tokens * mult),
        max_retrieval_rounds=config.max_retrieval_rounds,
        cost_limit=config.cost_limit_per_candidate * mult,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------
# OrchestratorDecision
# ---------------------------------------------------------------------------


class OrchestratorDecision(BaseModel):
    """The Orchestrator's routing decision for a single candidate.

    Contains everything the downstream agent chain needs to begin processing:
    the candidate reference, assigned risk, tool budget, and model assignments.

    Attributes:
        candidate_id:   64-char SHA-256 of the routed candidate.
        run_id:         Governed run identifier.
        schema_version: Schema version_hash at orchestration time.
        risk_class:     Assigned risk classification.
        budget:         Token/cost/timeout budget for the agent chain.
        model_a:        OpenRouter model ID for Agent-A.
        model_b:        OpenRouter model ID for Agent-B.
        model_p:        OpenRouter model ID for Agent-P.
    """

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    run_id: str
    schema_version: str
    risk_class: RiskClass
    budget: ToolBudget
    model_a: str
    model_b: str
    model_p: str


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class OrchestratorError(Exception):
    """Raised when the orchestrator cannot process the batch."""


class Orchestrator:
    """Non-LLM orchestrator that routes candidates to the agent chain.

    Usage:
        orchestrator = Orchestrator(config=agent_config)
        decisions = orchestrator.orchestrate(candidates)
    """

    def __init__(
        self,
        config: AgentConfig | None = None,
        timeout_per_candidate_s: float = DEFAULT_TIMEOUT_PER_CANDIDATE_S,
    ) -> None:
        self._config = config or AgentConfig()
        self._timeout_s = timeout_per_candidate_s

    def orchestrate(
        self,
        candidates: list[Candidate],
        proposal_class_hints: dict[str, str] | None = None,
    ) -> list[OrchestratorDecision]:
        """Assign risk class and budgets to a batch of candidates.

        Args:
            candidates:            Candidates to process (max MAX_CANDIDATES_PER_BATCH).
            proposal_class_hints:  Optional mapping of candidate_id -> anticipated
                                   proposal_class string.  Used for risk escalation
                                   when a prior heuristic suggests the likely action.

        Returns:
            List of OrchestratorDecision, one per input candidate, in the same
            order as the input list.

        Raises:
            OrchestratorError: If the batch exceeds MAX_CANDIDATES_PER_BATCH.
        """
        if len(candidates) > MAX_CANDIDATES_PER_BATCH:
            raise OrchestratorError(
                f"Batch size {len(candidates)} exceeds maximum "
                f"{MAX_CANDIDATES_PER_BATCH}. Slice input before calling orchestrate()."
            )

        hints = proposal_class_hints or {}
        decisions: list[OrchestratorDecision] = []

        for candidate in candidates:
            hint = hints.get(candidate.candidate_id)
            risk = assign_risk_class(candidate, proposal_class_hint=hint)
            budget = compute_budget(
                risk=risk,
                config=self._config,
                timeout_s=self._timeout_s,
            )

            decision = OrchestratorDecision(
                candidate_id=candidate.candidate_id,
                run_id=candidate.run_id,
                schema_version=candidate.schema_version,
                risk_class=risk,
                budget=budget,
                model_a=self._config.agent_a.model,
                model_b=self._config.agent_b.model,
                model_p=self._config.agent_p.model,
            )
            decisions.append(decision)

            logger.info(
                "orchestrator_decision",
                candidate_id=candidate.candidate_id,
                candidate_type=str(candidate.candidate_type),
                risk_class=str(risk),
                cost_limit=budget.cost_limit,
                timeout_s=budget.timeout_s,
                run_id=candidate.run_id,
            )

        logger.info(
            "orchestrator_batch_complete",
            batch_size=len(decisions),
            risk_distribution={
                "low": sum(1 for d in decisions if d.risk_class == RiskClass.low),
                "medium": sum(1 for d in decisions if d.risk_class == RiskClass.medium),
                "high": sum(1 for d in decisions if d.risk_class == RiskClass.high),
            },
        )

        return decisions
