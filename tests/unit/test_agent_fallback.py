"""
tests/unit/test_agent_fallback.py — Agent fallback and failure-path tests
(SPEC-07 file 17).

No network, no LLM, no Neo4j.  All LLM responses are mocked.  Tests verify:

  Agent-A (EvidenceAssemblyAgent):
    - Malformed LLM response → fallback EvidenceReport with sufficient=False
    - LLM returning None → fallback EvidenceReport with sufficient=False
    - Telemetry recorded even on failure path

  Agent-B (RetrievalAugmentationAgent):
    - Budget exhaustion (budget_remaining=0) → loop exits immediately
    - All rounds fail (LLM returns None each round) → returns original report
    - Telemetry recorded for each round attempt

  Agent-P (ProposalComposerAgent):
    - Cypher injection in rationale → rejected (returns None)
    - Cypher injection in rule_ids → rejected (returns None)
    - Executable code in rationale → rejected (returns None)
    - Malformed LLM output → returns None (fail-closed)
    - Telemetry recorded even on safety violation path

  Pipeline continuity:
    - After Agent-A failure, pipeline can still proceed (Agent-B receives
      fallback report with sufficient=False)
    - After Agent-P safety rejection, pipeline terminates cleanly with
      telemetry recorded

Fixtures:
  - fixtures/agent_pipeline/sample_candidate.json
  - fixtures/agent_pipeline/sample_evidence_report.json
  - fixtures/agent_pipeline/sample_agent_proposal.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.agents.models import (
    AgentProposalOutput,
    EvidenceClassification,
    EvidenceItem,
    EvidenceReport,
    RetrievalResult,
)
from api.agents.orchestrator import (
    Orchestrator,
    OrchestratorDecision,
    RiskClass,
    ToolBudget,
)
from api.config import AgentConfig
from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity
from api.observability.metrics import MetricsCollector, get_metrics

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "agent_pipeline"
_HEX64_A = "a" * 64
_HEX64_B = "b" * 64
_HEX64_C = "c" * 64
_RUN_ID = "test-run-agent-fallback-001"
_SCHEMA_VERSION = "sv_agent_fixture_abc123"


# ---------------------------------------------------------------------------
# Fixture loaders
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict[str, Any]:
    path = _FIXTURES / name
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_candidate() -> Candidate:
    data = _load_fixture("sample_candidate.json")
    data.pop("_expected_candidate_id", None)
    return Candidate(**data)


def _load_evidence_report() -> EvidenceReport:
    data = _load_fixture("sample_evidence_report.json")
    return EvidenceReport(**data)


def _load_agent_proposal() -> AgentProposalOutput:
    data = _load_fixture("sample_agent_proposal.json")
    return AgentProposalOutput(**data)


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _make_candidate(**overrides: object) -> Candidate:
    defaults: dict = dict(
        run_id=_RUN_ID,
        schema_version=_SCHEMA_VERSION,
        candidate_type=CandidateType.exact_node_duplicate,
        candidate_lane=CandidateLane.node,
        involved_element_refs=(
            "Person|Jane Doe|sv_agent_fixture_abc123",
            "Person|Jane M Doe|sv_agent_fixture_abc123",
        ),
        severity=Severity.high,
        detection_method="exact_node_dedupe_key",
        collision_context={},
    )
    return Candidate(**{**defaults, **overrides})


def _make_decision(candidate: Candidate) -> OrchestratorDecision:
    config = AgentConfig()
    orch = Orchestrator(config=config)
    decisions = orch.orchestrate([candidate])
    return decisions[0]


def _make_evidence_report(
    candidate: Candidate,
    decision: OrchestratorDecision,
    **overrides: object,
) -> EvidenceReport:
    defaults: dict = dict(
        candidate_id=candidate.candidate_id,
        items=(
            EvidenceItem(
                chunk_id=_HEX64_A,
                source_doc="doc_001",
                page_locator="p1:c0",
                classification=EvidenceClassification.supporting,
                relevance_score=0.9,
            ),
        ),
        sufficiency_score=0.3,
        sufficient=False,
        run_id=decision.run_id,
        schema_version=decision.schema_version,
    )
    return EvidenceReport(**{**defaults, **overrides})


def _fresh_metrics() -> MetricsCollector:
    """Return a fresh MetricsCollector to isolate test telemetry."""
    return MetricsCollector()


# ===========================================================================
# Fixture validation
# ===========================================================================


class TestFixtureIntegrity:
    """Verify fixtures match their Pydantic models (fail-fast on drift)."""

    def test_sample_candidate_loads(self) -> None:
        candidate = _load_candidate()
        assert len(candidate.candidate_id) == 64
        assert candidate.run_id == _RUN_ID

    def test_sample_candidate_id_matches_expected(self) -> None:
        data = _load_fixture("sample_candidate.json")
        candidate = _load_candidate()
        assert candidate.candidate_id == data["_expected_candidate_id"]

    def test_sample_evidence_report_loads(self) -> None:
        report = _load_evidence_report()
        assert report.sufficient is True
        assert len(report.items) == 3
        classifications = {item.classification for item in report.items}
        assert classifications == {
            EvidenceClassification.supporting,
            EvidenceClassification.corroborating,
            EvidenceClassification.conflicting,
        }

    def test_sample_agent_proposal_loads(self) -> None:
        proposal = _load_agent_proposal()
        assert proposal.proposal_class == "merge"
        assert len(proposal.rule_ids) == 2
        assert len(proposal.evidence_ids) == 2
        assert proposal.confidence_score == 0.88

    def test_evidence_report_candidate_id_matches(self) -> None:
        candidate = _load_candidate()
        report = _load_evidence_report()
        assert report.candidate_id == candidate.candidate_id

    def test_agent_proposal_candidate_id_matches(self) -> None:
        candidate = _load_candidate()
        proposal = _load_agent_proposal()
        assert proposal.candidate_id == candidate.candidate_id


# ===========================================================================
# Agent-A: EvidenceAssemblyAgent fallback tests
# ===========================================================================


class TestEvidenceAssemblyFallback:
    """Agent-A malformed/None LLM → fallback EvidenceReport (sufficient=False)."""

    @pytest.mark.asyncio
    async def test_none_llm_response_produces_fallback(self) -> None:
        """LLM returns None → Agent-A emits fallback with sufficient=False."""
        from api.agents.evidence import EvidenceAssemblyAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(return_value=None)

        mock_reader = MagicMock()
        mock_reader.get_neighbors = AsyncMock(
            return_value=MagicMock(neighbors=[]),
        )

        mock_retriever = MagicMock()
        mock_retriever.by_dedupe_key = AsyncMock(return_value=[])

        mock_cache = MagicMock()
        mock_cache.set = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.evidence.get_metrics", return_value=metrics):
            agent = EvidenceAssemblyAgent(
                llm_client=mock_llm,
                graph_reader=mock_reader,
                retriever=mock_retriever,
                cache=mock_cache,
            )
            report = await agent.run(candidate=candidate, decision=decision)

        assert report.sufficient is False
        assert report.sufficiency_score == 0.0
        assert len(report.items) == 0
        assert report.candidate_id == candidate.candidate_id
        assert report.run_id == decision.run_id
        assert report.schema_version == decision.schema_version

    @pytest.mark.asyncio
    async def test_fallback_records_telemetry(self) -> None:
        """Telemetry is recorded even when LLM returns None."""
        from api.agents.evidence import EvidenceAssemblyAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(return_value=None)

        mock_reader = MagicMock()
        mock_reader.get_neighbors = AsyncMock(
            return_value=MagicMock(neighbors=[]),
        )

        mock_retriever = MagicMock()
        mock_retriever.by_dedupe_key = AsyncMock(return_value=[])

        mock_cache = MagicMock()
        mock_cache.set = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.evidence.get_metrics", return_value=metrics):
            agent = EvidenceAssemblyAgent(
                llm_client=mock_llm,
                graph_reader=mock_reader,
                retriever=mock_retriever,
                cache=mock_cache,
            )
            await agent.run(candidate=candidate, decision=decision)

        snapshot = metrics.snapshot()
        # agent_calls counter should be incremented
        agent_key = f"agent_calls{{agent=agent-a,candidate_id={candidate.candidate_id},run_id={_RUN_ID}}}"
        assert snapshot["counters"].get(agent_key, 0) >= 1

    @pytest.mark.asyncio
    async def test_graph_context_error_degrades_gracefully(self) -> None:
        """Graph reader errors → empty context, agent still runs."""
        from api.agents.evidence import EvidenceAssemblyAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(return_value=None)

        mock_reader = MagicMock()
        mock_reader.get_neighbors = AsyncMock(
            side_effect=RuntimeError("Neo4j unavailable"),
        )

        mock_retriever = MagicMock()
        mock_retriever.by_dedupe_key = AsyncMock(return_value=[])

        mock_cache = MagicMock()
        mock_cache.set = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.evidence.get_metrics", return_value=metrics):
            agent = EvidenceAssemblyAgent(
                llm_client=mock_llm,
                graph_reader=mock_reader,
                retriever=mock_retriever,
                cache=mock_cache,
            )
            report = await agent.run(candidate=candidate, decision=decision)

        # Fallback report — the agent must not crash.
        assert report.sufficient is False
        assert report.candidate_id == candidate.candidate_id

    @pytest.mark.asyncio
    async def test_chunk_retrieval_error_degrades_gracefully(self) -> None:
        """Qdrant retrieval error → no chunks, agent still runs."""
        from api.agents.evidence import EvidenceAssemblyAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(return_value=None)

        mock_reader = MagicMock()
        mock_reader.get_neighbors = AsyncMock(
            return_value=MagicMock(neighbors=[]),
        )

        mock_retriever = MagicMock()
        mock_retriever.by_dedupe_key = AsyncMock(
            side_effect=RuntimeError("Qdrant unavailable"),
        )

        mock_cache = MagicMock()
        mock_cache.set = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.evidence.get_metrics", return_value=metrics):
            agent = EvidenceAssemblyAgent(
                llm_client=mock_llm,
                graph_reader=mock_reader,
                retriever=mock_retriever,
                cache=mock_cache,
            )
            report = await agent.run(candidate=candidate, decision=decision)

        assert report.sufficient is False
        assert report.candidate_id == candidate.candidate_id


# ===========================================================================
# Agent-B: RetrievalAugmentationAgent fallback tests
# ===========================================================================


class TestRetrievalAugmentationFallback:
    """Agent-B budget exhaustion and LLM failure paths."""

    @pytest.mark.asyncio
    async def test_budget_exhausted_stops_after_first_round(self) -> None:
        """Minimal budget (1 token) → first round exhausts it, second round skipped.

        ToolBudget requires max_input_tokens_b > 0 (Pydantic gt=0).  Setting it
        to 1 means budget_remaining starts at 1 — enough for one round, but
        _PER_ROUND_TOKEN_COST (500) exceeds it, so budget drops to 0 and the
        loop exits before a second round.
        """
        from api.agents.retrieval import RetrievalAugmentationAgent

        candidate = _make_candidate()
        config = AgentConfig()
        decision = OrchestratorDecision(
            candidate_id=candidate.candidate_id,
            run_id=_RUN_ID,
            schema_version=_SCHEMA_VERSION,
            risk_class=RiskClass.medium,
            budget=ToolBudget(
                max_input_tokens_a=4000,
                max_output_tokens_a=1000,
                max_input_tokens_b=1,   # minimal budget — exhausts after round 1
                max_output_tokens_b=500,
                max_input_tokens_p=4000,
                max_output_tokens_p=1000,
                max_retrieval_rounds=5,
                cost_limit=0.05,
                timeout_s=60.0,
            ),
            model_a=config.agent_a.model,
            model_b=config.agent_b.model,
            model_p=config.agent_p.model,
        )

        initial_report = _make_evidence_report(candidate, decision)

        # LLM returns a valid query so the first round executes.
        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(
            return_value=RetrievalResult(
                query="find more evidence",
                chunks_retrieved=(),
                rounds_used=0,
                budget_remaining=0,
            ),
        )

        mock_retriever = MagicMock()
        mock_retriever.by_query = AsyncMock(return_value=[])

        mock_cache = MagicMock()
        mock_cache.set = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.retrieval.get_metrics", return_value=metrics):
            agent = RetrievalAugmentationAgent(
                llm_client=mock_llm,
                retriever=mock_retriever,
                cache=mock_cache,
            )
            updated_report, rounds = await agent.run(
                candidate=candidate,
                decision=decision,
                evidence_report=initial_report,
            )

        # First round executes, budget drops to 0, loop exits before round 2.
        assert mock_llm.call.call_count == 1
        assert len(rounds) == 1
        # Report items preserved from input (no new chunks found).
        assert len(updated_report.items) == len(initial_report.items)

    @pytest.mark.asyncio
    async def test_all_rounds_fail_returns_original_report(self) -> None:
        """LLM returns None every round → returns report with original items."""
        from api.agents.retrieval import RetrievalAugmentationAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)
        initial_report = _make_evidence_report(candidate, decision)

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(return_value=None)

        mock_retriever = MagicMock()
        mock_retriever.by_query = AsyncMock(return_value=[])

        mock_cache = MagicMock()
        mock_cache.set = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.retrieval.get_metrics", return_value=metrics):
            agent = RetrievalAugmentationAgent(
                llm_client=mock_llm,
                retriever=mock_retriever,
                cache=mock_cache,
            )
            updated_report, rounds = await agent.run(
                candidate=candidate,
                decision=decision,
                evidence_report=initial_report,
            )

        # LLM was called once (first round) then broke out on None result.
        assert mock_llm.call.call_count == 1
        assert len(rounds) == 0
        # Original items preserved (no new chunks added).
        assert len(updated_report.items) == len(initial_report.items)
        assert updated_report.candidate_id == candidate.candidate_id

    @pytest.mark.asyncio
    async def test_max_rounds_enforced(self) -> None:
        """Agent-B stops after max_retrieval_rounds even with positive budget."""
        from api.agents.retrieval import RetrievalAugmentationAgent

        candidate = _make_candidate()
        config = AgentConfig(max_retrieval_rounds=2)
        orch = Orchestrator(config=config)
        decision = orch.orchestrate([candidate])[0]

        # Give it an insufficient report so it keeps trying.
        initial_report = _make_evidence_report(
            candidate, decision, sufficiency_score=0.1, sufficient=False,
        )

        # LLM returns a valid query each round but retriever returns no new chunks.
        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(
            return_value=RetrievalResult(
                query="find more about Jane Doe",
                chunks_retrieved=(),
                rounds_used=0,
                budget_remaining=5000,
            ),
        )

        mock_retriever = MagicMock()
        mock_retriever.by_query = AsyncMock(return_value=[])

        mock_cache = MagicMock()
        mock_cache.set = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.retrieval.get_metrics", return_value=metrics):
            agent = RetrievalAugmentationAgent(
                llm_client=mock_llm,
                retriever=mock_retriever,
                cache=mock_cache,
            )
            _, rounds = await agent.run(
                candidate=candidate,
                decision=decision,
                evidence_report=initial_report,
            )

        # Should have exactly max_retrieval_rounds rounds.
        assert len(rounds) == 2
        assert mock_llm.call.call_count == 2

    @pytest.mark.asyncio
    async def test_retrieval_records_telemetry_per_round(self) -> None:
        """Telemetry is recorded for each retrieval round."""
        from api.agents.retrieval import RetrievalAugmentationAgent

        candidate = _make_candidate()
        config = AgentConfig(max_retrieval_rounds=2)
        orch = Orchestrator(config=config)
        decision = orch.orchestrate([candidate])[0]

        initial_report = _make_evidence_report(
            candidate, decision, sufficiency_score=0.1, sufficient=False,
        )

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(
            return_value=RetrievalResult(
                query="search query",
                chunks_retrieved=(),
                rounds_used=0,
                budget_remaining=5000,
            ),
        )

        mock_retriever = MagicMock()
        mock_retriever.by_query = AsyncMock(return_value=[])

        mock_cache = MagicMock()
        mock_cache.set = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.retrieval.get_metrics", return_value=metrics):
            agent = RetrievalAugmentationAgent(
                llm_client=mock_llm,
                retriever=mock_retriever,
                cache=mock_cache,
            )
            await agent.run(
                candidate=candidate,
                decision=decision,
                evidence_report=initial_report,
            )

        snapshot = metrics.snapshot()
        agent_key = f"agent_calls{{agent=agent-b,candidate_id={candidate.candidate_id},run_id={_RUN_ID}}}"
        assert snapshot["counters"].get(agent_key, 0) == 2


# ===========================================================================
# Agent-P: ProposalComposerAgent fallback tests
# ===========================================================================


class TestProposalComposerCypherInjection:
    """Agent-P rejects output containing Cypher query syntax."""

    def test_cypher_match_in_rationale_rejected(self) -> None:
        """MATCH (n:Person) pattern in rationale → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="merge",
            rule_ids=("node:Person",),
            evidence_ids=(_HEX64_A,),
            rationale="We should MATCH (n:Person {name:'Jane'}) and merge.",
            confidence_score=0.9,
        )
        with pytest.raises(ProposalSafetyError, match="Cypher"):
            _guard_no_cypher_or_executable(output)

    def test_cypher_create_in_rationale_rejected(self) -> None:
        """CREATE (n:Label {}) pattern → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="canonicalize",
            rule_ids=(),
            evidence_ids=(),
            rationale="First CREATE (n:Person {name:'New'}) then update.",
            confidence_score=0.8,
        )
        with pytest.raises(ProposalSafetyError, match="Cypher"):
            _guard_no_cypher_or_executable(output)

    def test_cypher_merge_pattern_in_rationale_rejected(self) -> None:
        """MERGE (n:Label) pattern → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="merge",
            rule_ids=(),
            evidence_ids=(),
            rationale="Please MERGE (n:Person)-[:KNOWS]->(m:Person) to fix.",
            confidence_score=0.7,
        )
        with pytest.raises(ProposalSafetyError, match="Cypher"):
            _guard_no_cypher_or_executable(output)

    def test_cypher_detach_delete_in_rationale_rejected(self) -> None:
        """DETACH DELETE pattern → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="delete",
            rule_ids=(),
            evidence_ids=(),
            rationale="Should DETACH DELETE the orphan node to clean up.",
            confidence_score=0.85,
        )
        with pytest.raises(ProposalSafetyError, match="Cypher"):
            _guard_no_cypher_or_executable(output)

    def test_cypher_set_property_in_rationale_rejected(self) -> None:
        """SET n.prop pattern → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="rename",
            rule_ids=(),
            evidence_ids=(),
            rationale="Rename by SET n.name = 'Jane Doe' on the canonical node.",
            confidence_score=0.9,
        )
        with pytest.raises(ProposalSafetyError, match="Cypher"):
            _guard_no_cypher_or_executable(output)

    def test_cypher_in_rule_ids_rejected(self) -> None:
        """Cypher syntax injected into rule_ids → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="canonicalize",
            rule_ids=("MATCH (n:Person)", "node:Person"),
            evidence_ids=(),
            rationale="Standard canonicalization per schema rules.",
            confidence_score=0.9,
        )
        with pytest.raises(ProposalSafetyError, match="Cypher"):
            _guard_no_cypher_or_executable(output)

    def test_clean_rationale_passes(self) -> None:
        """Normal English text (even with 'merge', 'match' words) passes."""
        from api.agents.proposal import _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="merge",
            rule_ids=("node:Person",),
            evidence_ids=(_HEX64_A,),
            rationale="These entities match and should be merged into one canonical form.",
            confidence_score=0.9,
        )
        # Should not raise.
        _guard_no_cypher_or_executable(output)


class TestProposalComposerExecutableInjection:
    """Agent-P rejects output containing executable code patterns."""

    def test_exec_in_rationale_rejected(self) -> None:
        """exec() call in rationale → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="canonicalize",
            rule_ids=(),
            evidence_ids=(),
            rationale="Apply exec('import os; os.remove(db)') to fix.",
            confidence_score=0.8,
        )
        with pytest.raises(ProposalSafetyError, match="executable"):
            _guard_no_cypher_or_executable(output)

    def test_eval_in_rationale_rejected(self) -> None:
        """eval() call in rationale → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="canonicalize",
            rule_ids=(),
            evidence_ids=(),
            rationale="Use eval('2+2') for the correction.",
            confidence_score=0.8,
        )
        with pytest.raises(ProposalSafetyError, match="executable"):
            _guard_no_cypher_or_executable(output)

    def test_code_block_in_rationale_rejected(self) -> None:
        """Markdown code block (```) in rationale → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="normalize",
            rule_ids=(),
            evidence_ids=(),
            rationale="Apply this fix:\n```python\nprint('hello')\n```",
            confidence_score=0.7,
        )
        with pytest.raises(ProposalSafetyError, match="executable"):
            _guard_no_cypher_or_executable(output)

    def test_subprocess_in_rationale_rejected(self) -> None:
        """subprocess.run reference in rationale → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="canonicalize",
            rule_ids=(),
            evidence_ids=(),
            rationale="Run subprocess.run to update the database directly.",
            confidence_score=0.6,
        )
        with pytest.raises(ProposalSafetyError, match="executable"):
            _guard_no_cypher_or_executable(output)

    def test_os_system_in_rationale_rejected(self) -> None:
        """os.system() in rationale → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="delete",
            rule_ids=(),
            evidence_ids=(),
            rationale="Clean up with os.system('rm -rf /data').",
            confidence_score=0.5,
        )
        with pytest.raises(ProposalSafetyError, match="executable"):
            _guard_no_cypher_or_executable(output)

    def test_dunder_import_in_rationale_rejected(self) -> None:
        """__import__() in rationale → ProposalSafetyError."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="canonicalize",
            rule_ids=(),
            evidence_ids=(),
            rationale="Use __import__('os').unlink('/tmp/data').",
            confidence_score=0.4,
        )
        with pytest.raises(ProposalSafetyError, match="executable"):
            _guard_no_cypher_or_executable(output)


class TestProposalComposerMalformedOutput:
    """Agent-P LLM returning None or malformed output → fail-closed."""

    @pytest.mark.asyncio
    async def test_none_llm_response_returns_none(self) -> None:
        """LLM returns None → Agent-P returns None (fail-closed)."""
        from api.agents.proposal import ProposalComposerAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)
        evidence_report = _make_evidence_report(candidate, decision, sufficient=True)

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(return_value=None)

        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=None)  # schema not found

        metrics = _fresh_metrics()
        with patch("api.agents.proposal.get_metrics", return_value=metrics):
            agent = ProposalComposerAgent(
                llm_client=mock_llm,
                cache=mock_cache,
            )
            result = await agent.run(
                candidate=candidate,
                decision=decision,
                evidence_report=evidence_report,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_none_llm_records_telemetry(self) -> None:
        """Telemetry recorded even when LLM returns None."""
        from api.agents.proposal import ProposalComposerAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)
        evidence_report = _make_evidence_report(candidate, decision, sufficient=True)

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(return_value=None)

        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.proposal.get_metrics", return_value=metrics):
            agent = ProposalComposerAgent(
                llm_client=mock_llm,
                cache=mock_cache,
            )
            await agent.run(
                candidate=candidate,
                decision=decision,
                evidence_report=evidence_report,
            )

        snapshot = metrics.snapshot()
        calls_key = f"agent_calls{{agent=agent-p,candidate_id={candidate.candidate_id},run_id={_RUN_ID}}}"
        assert snapshot["counters"].get(calls_key, 0) >= 1

        failed_key = f"agent_proposals_failed{{agent=agent-p,candidate_id={candidate.candidate_id},run_id={_RUN_ID}}}"
        assert snapshot["counters"].get(failed_key, 0) >= 1

    @pytest.mark.asyncio
    async def test_cypher_injection_returns_none(self) -> None:
        """LLM returns output with Cypher → Agent-P returns None."""
        from api.agents.proposal import ProposalComposerAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)
        evidence_report = _make_evidence_report(candidate, decision, sufficient=True)

        poisoned_output = AgentProposalOutput(
            candidate_id=candidate.candidate_id,
            proposal_class="merge",
            rule_ids=("node:Person",),
            evidence_ids=(_HEX64_A,),
            rationale="MATCH (n:Person {name:'Jane'}) to merge the nodes.",
            confidence_score=0.9,
        )

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(return_value=poisoned_output)

        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.proposal.get_metrics", return_value=metrics):
            agent = ProposalComposerAgent(
                llm_client=mock_llm,
                cache=mock_cache,
            )
            result = await agent.run(
                candidate=candidate,
                decision=decision,
                evidence_report=evidence_report,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_cypher_injection_records_failure_telemetry(self) -> None:
        """Cypher injection → agent_proposals_failed counter incremented."""
        from api.agents.proposal import ProposalComposerAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)
        evidence_report = _make_evidence_report(candidate, decision, sufficient=True)

        poisoned_output = AgentProposalOutput(
            candidate_id=candidate.candidate_id,
            proposal_class="merge",
            rule_ids=("node:Person",),
            evidence_ids=(_HEX64_A,),
            rationale="MERGE (n:Person)-[:KNOWS]->(m) to fix the issue.",
            confidence_score=0.9,
        )

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(return_value=poisoned_output)

        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.proposal.get_metrics", return_value=metrics):
            agent = ProposalComposerAgent(
                llm_client=mock_llm,
                cache=mock_cache,
            )
            await agent.run(
                candidate=candidate,
                decision=decision,
                evidence_report=evidence_report,
            )

        snapshot = metrics.snapshot()
        failed_key = f"agent_proposals_failed{{agent=agent-p,candidate_id={candidate.candidate_id},run_id={_RUN_ID}}}"
        assert snapshot["counters"].get(failed_key, 0) >= 1

    @pytest.mark.asyncio
    async def test_executable_injection_returns_none(self) -> None:
        """LLM returns output with exec() → Agent-P returns None."""
        from api.agents.proposal import ProposalComposerAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)
        evidence_report = _make_evidence_report(candidate, decision, sufficient=True)

        poisoned_output = AgentProposalOutput(
            candidate_id=candidate.candidate_id,
            proposal_class="canonicalize",
            rule_ids=(),
            evidence_ids=(),
            rationale="Apply the fix via exec('import shutil; shutil.rmtree(db)').",
            confidence_score=0.8,
        )

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(return_value=poisoned_output)

        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.proposal.get_metrics", return_value=metrics):
            agent = ProposalComposerAgent(
                llm_client=mock_llm,
                cache=mock_cache,
            )
            result = await agent.run(
                candidate=candidate,
                decision=decision,
                evidence_report=evidence_report,
            )

        assert result is None


# ===========================================================================
# Pipeline continuity after individual agent failures
# ===========================================================================


class TestPipelineContinuity:
    """Pipeline can continue correctly after individual agent failures."""

    @pytest.mark.asyncio
    async def test_agent_a_failure_produces_usable_fallback_for_agent_b(self) -> None:
        """Agent-A failure → fallback report (sufficient=False) → Agent-B can consume it."""
        from api.agents.evidence import EvidenceAssemblyAgent
        from api.agents.retrieval import RetrievalAugmentationAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)

        # Agent-A: LLM returns None → produces fallback.
        mock_llm_a = MagicMock()
        mock_llm_a.call = AsyncMock(return_value=None)

        mock_reader = MagicMock()
        mock_reader.get_neighbors = AsyncMock(
            return_value=MagicMock(neighbors=[]),
        )

        mock_retriever = MagicMock()
        mock_retriever.by_dedupe_key = AsyncMock(return_value=[])
        mock_retriever.by_query = AsyncMock(return_value=[])

        mock_cache = MagicMock()
        mock_cache.set = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.evidence.get_metrics", return_value=metrics):
            agent_a = EvidenceAssemblyAgent(
                llm_client=mock_llm_a,
                graph_reader=mock_reader,
                retriever=mock_retriever,
                cache=mock_cache,
            )
            fallback_report = await agent_a.run(candidate=candidate, decision=decision)

        assert fallback_report.sufficient is False
        assert fallback_report.sufficiency_score == 0.0

        # Agent-B: receives the fallback report — should not crash.
        mock_llm_b = MagicMock()
        mock_llm_b.call = AsyncMock(return_value=None)  # also fails

        with patch("api.agents.retrieval.get_metrics", return_value=metrics):
            agent_b = RetrievalAugmentationAgent(
                llm_client=mock_llm_b,
                retriever=mock_retriever,
                cache=mock_cache,
            )
            updated_report, rounds = await agent_b.run(
                candidate=candidate,
                decision=decision,
                evidence_report=fallback_report,
            )

        # Agent-B ran without crashing.
        assert updated_report.candidate_id == candidate.candidate_id
        assert updated_report.run_id == _RUN_ID

    @pytest.mark.asyncio
    async def test_agent_p_safety_rejection_terminates_cleanly(self) -> None:
        """Agent-P safety guard triggers → returns None, telemetry recorded."""
        from api.agents.proposal import ProposalComposerAgent

        candidate = _make_candidate()
        decision = _make_decision(candidate)
        report = _make_evidence_report(candidate, decision, sufficient=True)

        poisoned_output = AgentProposalOutput(
            candidate_id=candidate.candidate_id,
            proposal_class="delete",
            rule_ids=(),
            evidence_ids=(),
            rationale="DETACH DELETE this orphan node from the graph.",
            confidence_score=0.95,
        )

        mock_llm = MagicMock()
        mock_llm.call = AsyncMock(return_value=poisoned_output)

        mock_cache = MagicMock()
        mock_cache.get = AsyncMock(return_value=None)

        metrics = _fresh_metrics()
        with patch("api.agents.proposal.get_metrics", return_value=metrics):
            agent = ProposalComposerAgent(
                llm_client=mock_llm,
                cache=mock_cache,
            )
            result = await agent.run(
                candidate=candidate,
                decision=decision,
                evidence_report=report,
            )

        assert result is None

        # Telemetry shows failure.
        snapshot = metrics.snapshot()
        failed_key = f"agent_proposals_failed{{agent=agent-p,candidate_id={candidate.candidate_id},run_id={_RUN_ID}}}"
        assert snapshot["counters"].get(failed_key, 0) >= 1

    def test_safety_guard_field_name_reported(self) -> None:
        """ProposalSafetyError includes the offending field name."""
        from api.agents.proposal import ProposalSafetyError, _guard_no_cypher_or_executable

        output = AgentProposalOutput(
            candidate_id=_HEX64_A,
            proposal_class="merge",
            rule_ids=("MATCH (x:Node)",),
            evidence_ids=(),
            rationale="Clean merge of duplicate nodes.",
            confidence_score=0.9,
        )

        with pytest.raises(ProposalSafetyError) as exc_info:
            _guard_no_cypher_or_executable(output)

        assert exc_info.value.field == "rule_ids[0]"
        assert exc_info.value.pattern_name == "Cypher query syntax"

    def test_safety_guard_clean_output_passes(self) -> None:
        """A fully clean AgentProposalOutput passes all safety guards."""
        from api.agents.proposal import _guard_no_cypher_or_executable

        output = _load_agent_proposal()
        # Should not raise — fixture has clean rationale.
        _guard_no_cypher_or_executable(output)
