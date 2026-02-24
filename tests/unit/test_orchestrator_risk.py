"""
tests/unit/test_orchestrator_risk.py — Orchestrator risk assignment, budget
allocation, and loop guards (SPEC-07 file 15).

No network, no LLM, no Neo4j.  Tests are purely deterministic, verifying:

  - Risk class assignment per candidate type (_TYPE_RISK mapping)
  - Proposal class hint escalation: merge/delete → high, rename/normalize → medium,
    canonicalize/defer → low.  Risk = max(type_risk, escalation).
  - Budget allocation proportional to risk via AgentConfig multipliers
    (low=0.5×, medium=1.0×, high=1.5×)
  - Loop guard: batch > MAX_CANDIDATES_PER_BATCH raises OrchestratorError
  - Determinism: same inputs → same outputs, order preserved
  - OrchestratorDecision carries correct model assignments, run_id, schema_version
"""

from __future__ import annotations

import pytest

from api.agents.orchestrator import (
    MAX_CANDIDATES_PER_BATCH,
    Orchestrator,
    OrchestratorError,
    RiskClass,
    ToolBudget,
    _BUDGET_MULTIPLIERS,
    assign_risk_class,
    compute_budget,
)
from api.config import AgentConfig, AgentModelConfig
from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------

_BASE: dict = dict(
    run_id="unit-test-run-orch-0001",
    schema_version="sv_orch_fixture_abc123",
    candidate_type=CandidateType.exact_node_duplicate,
    candidate_lane=CandidateLane.node,
    involved_element_refs=("ref_alpha", "ref_beta"),
    severity=Severity.high,
    detection_method="exact_node_dedupe_key",
    collision_context={},
)


def _make(**overrides: object) -> Candidate:
    return Candidate(**{**_BASE, **overrides})


def _default_config() -> AgentConfig:
    return AgentConfig()


# ===========================================================================
# Risk class per candidate type
# ===========================================================================


class TestRiskClassByCandidateType:
    """Verify _TYPE_RISK mapping: each CandidateType → expected default risk."""

    def test_exact_node_duplicate_medium(self) -> None:
        c = _make(candidate_type=CandidateType.exact_node_duplicate)
        assert assign_risk_class(c) == RiskClass.medium

    def test_exact_rel_duplicate_medium(self) -> None:
        c = _make(
            candidate_type=CandidateType.exact_rel_duplicate,
            candidate_lane=CandidateLane.relationship,
        )
        assert assign_risk_class(c) == RiskClass.medium

    def test_probable_duplicate_high(self) -> None:
        c = _make(candidate_type=CandidateType.probable_duplicate)
        assert assign_risk_class(c) == RiskClass.high

    def test_canonical_violation_medium(self) -> None:
        c = _make(
            candidate_type=CandidateType.canonical_violation,
            candidate_lane=CandidateLane.relationship,
        )
        assert assign_risk_class(c) == RiskClass.medium

    def test_structural_anomaly_low(self) -> None:
        c = _make(
            candidate_type=CandidateType.structural_anomaly,
            candidate_lane=CandidateLane.structural,
        )
        assert assign_risk_class(c) == RiskClass.low

    def test_all_types_covered(self) -> None:
        """Every CandidateType has a risk mapping — no KeyError fallback needed."""
        for ct in CandidateType:
            c = _make(candidate_type=ct, candidate_lane=CandidateLane.node)
            risk = assign_risk_class(c)
            assert isinstance(risk, RiskClass)


# ===========================================================================
# Proposal class hint escalation
# ===========================================================================


class TestProposalClassEscalation:
    """Proposal class hints escalate risk.  Risk = max(type_risk, hint_risk)."""

    def test_merge_escalates_to_high(self) -> None:
        c = _make(
            candidate_type=CandidateType.structural_anomaly,
            candidate_lane=CandidateLane.structural,
        )
        assert assign_risk_class(c, proposal_class_hint="merge") == RiskClass.high

    def test_delete_escalates_to_high(self) -> None:
        c = _make(
            candidate_type=CandidateType.structural_anomaly,
            candidate_lane=CandidateLane.structural,
        )
        assert assign_risk_class(c, proposal_class_hint="delete") == RiskClass.high

    def test_rename_escalates_low_to_medium(self) -> None:
        # structural_anomaly defaults to low; rename escalates to medium.
        c = _make(
            candidate_type=CandidateType.structural_anomaly,
            candidate_lane=CandidateLane.structural,
        )
        assert assign_risk_class(c, proposal_class_hint="rename") == RiskClass.medium

    def test_normalize_keeps_medium(self) -> None:
        # exact_node_dup = medium, normalize = medium → medium.
        c = _make(candidate_type=CandidateType.exact_node_duplicate)
        assert assign_risk_class(c, proposal_class_hint="normalize") == RiskClass.medium

    def test_canonicalize_does_not_downgrade(self) -> None:
        # exact_node_dup = medium, canonicalize = low → max is medium (no downgrade).
        c = _make(candidate_type=CandidateType.exact_node_duplicate)
        assert assign_risk_class(c, proposal_class_hint="canonicalize") == RiskClass.medium

    def test_defer_does_not_downgrade_high(self) -> None:
        # probable_dup = high, defer = low → max is high (no downgrade).
        c = _make(candidate_type=CandidateType.probable_duplicate)
        assert assign_risk_class(c, proposal_class_hint="defer") == RiskClass.high

    def test_no_hint_uses_type_risk_only(self) -> None:
        c = _make(
            candidate_type=CandidateType.structural_anomaly,
            candidate_lane=CandidateLane.structural,
        )
        assert assign_risk_class(c, proposal_class_hint=None) == RiskClass.low

    def test_unknown_hint_defaults_to_medium(self) -> None:
        # _PROPOSAL_CLASS_ESCALATION.get("unknown") → default medium.
        c = _make(
            candidate_type=CandidateType.structural_anomaly,
            candidate_lane=CandidateLane.structural,
        )
        assert assign_risk_class(c, proposal_class_hint="unknown_class") == RiskClass.medium

    def test_delete_on_already_high_stays_high(self) -> None:
        # probable_dup = high, delete = high → still high (idempotent escalation).
        c = _make(candidate_type=CandidateType.probable_duplicate)
        assert assign_risk_class(c, proposal_class_hint="delete") == RiskClass.high


# ===========================================================================
# Budget allocation
# ===========================================================================


class TestBudgetAllocation:
    """Budget derived from AgentConfig × risk multiplier."""

    def test_low_risk_half_budget(self) -> None:
        config = _default_config()
        budget = compute_budget(RiskClass.low, config)
        mult = _BUDGET_MULTIPLIERS["low"]  # 0.5
        assert budget.max_input_tokens_a == int(config.agent_a.max_input_tokens * mult)
        assert budget.max_output_tokens_a == int(config.agent_a.max_output_tokens * mult)
        assert budget.max_input_tokens_b == int(config.agent_b.max_input_tokens * mult)
        assert budget.max_output_tokens_b == int(config.agent_b.max_output_tokens * mult)
        assert budget.max_input_tokens_p == int(config.agent_p.max_input_tokens * mult)
        assert budget.max_output_tokens_p == int(config.agent_p.max_output_tokens * mult)
        assert budget.cost_limit == config.cost_limit_per_candidate * mult

    def test_medium_risk_full_budget(self) -> None:
        config = _default_config()
        budget = compute_budget(RiskClass.medium, config)
        assert budget.max_input_tokens_a == config.agent_a.max_input_tokens
        assert budget.max_output_tokens_a == config.agent_a.max_output_tokens
        assert budget.max_input_tokens_b == config.agent_b.max_input_tokens
        assert budget.max_output_tokens_b == config.agent_b.max_output_tokens
        assert budget.max_input_tokens_p == config.agent_p.max_input_tokens
        assert budget.max_output_tokens_p == config.agent_p.max_output_tokens
        assert budget.cost_limit == config.cost_limit_per_candidate

    def test_high_risk_150pct_budget(self) -> None:
        config = _default_config()
        budget = compute_budget(RiskClass.high, config)
        mult = _BUDGET_MULTIPLIERS["high"]  # 1.5
        assert budget.max_input_tokens_a == int(config.agent_a.max_input_tokens * mult)
        assert budget.max_output_tokens_a == int(config.agent_a.max_output_tokens * mult)
        assert budget.cost_limit == config.cost_limit_per_candidate * mult

    def test_retrieval_rounds_not_scaled(self) -> None:
        """max_retrieval_rounds comes directly from config — no multiplier."""
        config = _default_config()
        for risk in RiskClass:
            budget = compute_budget(risk, config)
            assert budget.max_retrieval_rounds == config.max_retrieval_rounds

    def test_custom_timeout(self) -> None:
        config = _default_config()
        budget = compute_budget(RiskClass.medium, config, timeout_s=60.0)
        assert budget.timeout_s == 60.0

    def test_custom_config_propagates(self) -> None:
        custom = AgentConfig(
            agent_a=AgentModelConfig(
                model="test/model", max_input_tokens=1000, max_output_tokens=500,
            ),
            agent_b=AgentModelConfig(
                model="test/model", max_input_tokens=2000, max_output_tokens=800,
            ),
            agent_p=AgentModelConfig(
                model="test/model", max_input_tokens=3000, max_output_tokens=1200,
            ),
            cost_limit_per_candidate=0.50,
            max_retrieval_rounds=5,
        )
        budget = compute_budget(RiskClass.medium, custom)
        assert budget.max_input_tokens_a == 1000
        assert budget.max_output_tokens_a == 500
        assert budget.max_input_tokens_b == 2000
        assert budget.max_output_tokens_b == 800
        assert budget.max_input_tokens_p == 3000
        assert budget.max_output_tokens_p == 1200
        assert budget.max_retrieval_rounds == 5
        assert budget.cost_limit == 0.50

    def test_budget_is_frozen(self) -> None:
        budget = compute_budget(RiskClass.medium, _default_config())
        with pytest.raises(Exception):
            budget.max_input_tokens_a = 9999  # type: ignore[misc]

    def test_budget_multipliers_are_exhaustive(self) -> None:
        """Every RiskClass has a corresponding multiplier entry."""
        for risk in RiskClass:
            assert risk.value in _BUDGET_MULTIPLIERS


# ===========================================================================
# Loop guard
# ===========================================================================


class TestLoopGuard:
    """Orchestrator rejects oversized batches (fail-closed)."""

    def test_max_batch_accepted(self) -> None:
        orch = Orchestrator()
        candidates = [
            _make(involved_element_refs=(f"ref_{i}",))
            for i in range(MAX_CANDIDATES_PER_BATCH)
        ]
        decisions = orch.orchestrate(candidates)
        assert len(decisions) == MAX_CANDIDATES_PER_BATCH

    def test_oversize_batch_rejected(self) -> None:
        orch = Orchestrator()
        candidates = [
            _make(involved_element_refs=(f"ref_{i}",))
            for i in range(MAX_CANDIDATES_PER_BATCH + 1)
        ]
        with pytest.raises(OrchestratorError):
            orch.orchestrate(candidates)

    def test_empty_batch_accepted(self) -> None:
        orch = Orchestrator()
        decisions = orch.orchestrate([])
        assert decisions == []

    def test_single_candidate_accepted(self) -> None:
        orch = Orchestrator()
        decisions = orch.orchestrate([_make()])
        assert len(decisions) == 1


# ===========================================================================
# Orchestrate — determinism and order preservation
# ===========================================================================


class TestOrchestrateDeterminism:
    """Same inputs → same OrchestratorDecision list, in the same order."""

    def test_deterministic_output(self) -> None:
        orch = Orchestrator()
        candidates = [
            _make(
                candidate_type=CandidateType.structural_anomaly,
                candidate_lane=CandidateLane.structural,
                involved_element_refs=("ref_a",),
            ),
            _make(
                candidate_type=CandidateType.probable_duplicate,
                involved_element_refs=("ref_b", "ref_c"),
            ),
        ]
        d1 = orch.orchestrate(candidates)
        d2 = orch.orchestrate(candidates)
        assert len(d1) == len(d2)
        for a, b in zip(d1, d2):
            assert a.candidate_id == b.candidate_id
            assert a.risk_class == b.risk_class
            assert a.budget == b.budget

    def test_output_order_matches_input(self) -> None:
        orch = Orchestrator()
        candidates = [
            _make(
                candidate_type=CandidateType.structural_anomaly,
                candidate_lane=CandidateLane.structural,
                involved_element_refs=("ref_first",),
            ),
            _make(
                candidate_type=CandidateType.probable_duplicate,
                involved_element_refs=("ref_second", "ref_third"),
            ),
            _make(
                candidate_type=CandidateType.exact_node_duplicate,
                involved_element_refs=("ref_fourth",),
            ),
        ]
        decisions = orch.orchestrate(candidates)
        for i, (dec, cand) in enumerate(zip(decisions, candidates)):
            assert dec.candidate_id == cand.candidate_id, (
                f"Decision[{i}] candidate_id does not match input[{i}]"
            )


# ===========================================================================
# Orchestrate — decision field correctness
# ===========================================================================


class TestOrchestratorDecisionFields:
    """OrchestratorDecision carries correct metadata from input + config."""

    def test_decision_carries_model_assignments(self) -> None:
        config = AgentConfig(
            agent_a=AgentModelConfig(
                model="test/a", max_input_tokens=100, max_output_tokens=50,
            ),
            agent_b=AgentModelConfig(
                model="test/b", max_input_tokens=200, max_output_tokens=100,
            ),
            agent_p=AgentModelConfig(
                model="test/p", max_input_tokens=300, max_output_tokens=150,
            ),
        )
        orch = Orchestrator(config=config)
        decisions = orch.orchestrate([_make()])
        assert decisions[0].model_a == "test/a"
        assert decisions[0].model_b == "test/b"
        assert decisions[0].model_p == "test/p"

    def test_decision_carries_run_id_and_schema(self) -> None:
        orch = Orchestrator()
        c = _make(run_id="run-xyz", schema_version="sv-123")
        decisions = orch.orchestrate([c])
        assert decisions[0].run_id == "run-xyz"
        assert decisions[0].schema_version == "sv-123"

    def test_hint_escalation_reflected_in_decision(self) -> None:
        orch = Orchestrator()
        c = _make(
            candidate_type=CandidateType.structural_anomaly,
            candidate_lane=CandidateLane.structural,
            involved_element_refs=("ref_x",),
        )
        hints = {c.candidate_id: "merge"}
        decisions = orch.orchestrate([c], proposal_class_hints=hints)
        assert decisions[0].risk_class == RiskClass.high

    def test_mixed_risk_batch(self) -> None:
        """A batch with varied candidate types produces varied risk classes."""
        orch = Orchestrator()
        candidates = [
            _make(
                candidate_type=CandidateType.structural_anomaly,
                candidate_lane=CandidateLane.structural,
                involved_element_refs=("ref_low",),
            ),
            _make(
                candidate_type=CandidateType.exact_node_duplicate,
                involved_element_refs=("ref_med",),
            ),
            _make(
                candidate_type=CandidateType.probable_duplicate,
                involved_element_refs=("ref_high",),
            ),
        ]
        decisions = orch.orchestrate(candidates)
        assert decisions[0].risk_class == RiskClass.low
        assert decisions[1].risk_class == RiskClass.medium
        assert decisions[2].risk_class == RiskClass.high

    def test_high_risk_budget_larger_than_low(self) -> None:
        """High-risk decisions receive strictly larger budgets than low-risk."""
        orch = Orchestrator()
        candidates = [
            _make(
                candidate_type=CandidateType.structural_anomaly,
                candidate_lane=CandidateLane.structural,
                involved_element_refs=("ref_lo",),
            ),
            _make(
                candidate_type=CandidateType.probable_duplicate,
                involved_element_refs=("ref_hi",),
            ),
        ]
        decisions = orch.orchestrate(candidates)
        low_budget = decisions[0].budget
        high_budget = decisions[1].budget
        assert high_budget.max_input_tokens_a > low_budget.max_input_tokens_a
        assert high_budget.cost_limit > low_budget.cost_limit
