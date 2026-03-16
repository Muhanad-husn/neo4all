"""
tests/unit/test_agent_output_contracts.py — Agent output contract validation
(SPEC-07 file 16).

No network, no LLM, no Neo4j.  Tests verify that all agent output Pydantic models
enforce strict contracts (fail-closed per CLAUDE.md §4.4):

  EvidenceItem:
    - chunk_id must be 64-char hex, source_doc non-empty, classification enum,
      relevance_score in [0.0, 1.0]

  EvidenceReport (Agent-A):
    - candidate_id 64-char hex, run_id/schema_version non-empty,
      sufficiency_score in [0.0, 1.0], items tuple, frozen

  RetrievalResult (Agent-B):
    - query non-empty, rounds_used >= 0, budget_remaining >= 0, frozen

  AgentProposalOutput (Agent-P):
    - candidate_id 64-char hex, proposal_class validated against ProposalClass enum,
      rationale non-empty, confidence_score in [0.0, 1.0], frozen
    - Never contains Cypher or executable instructions (model-level; safety guard
      tested separately in agent code)

Coverage:
  - Valid payloads construct without errors
  - Malformed payloads raise ValidationError
  - Missing required fields → fail-closed
  - Evidence classifications validated against enum
  - Models are frozen (immutable after construction)
  - Idempotent reconstruction via model_dump → model_validate
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.agents.models import (
    AgentProposalOutput,
    EvidenceClassification,
    EvidenceItem,
    EvidenceReport,
    RetrievalResult,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEX64 = "a" * 64  # Valid 64-char hex digest
_HEX64_ALT = "b" * 64
_RUN_ID = "unit-test-run-contracts-0001"
_SCHEMA_VERSION = "sv_contracts_fixture"


# ---------------------------------------------------------------------------
# Builder helpers
# ---------------------------------------------------------------------------


def _make_evidence_item(**overrides: object) -> EvidenceItem:
    defaults: dict = dict(
        chunk_id=_HEX64,
        source_doc="doc_001",
        page_locator="p.3",
        classification=EvidenceClassification.supporting,
        relevance_score=0.85,
    )
    return EvidenceItem(**{**defaults, **overrides})


def _make_evidence_report(**overrides: object) -> EvidenceReport:
    defaults: dict = dict(
        candidate_id=_HEX64,
        items=(_make_evidence_item(),),
        sufficiency_score=0.8,
        sufficient=True,
        run_id=_RUN_ID,
        schema_version=_SCHEMA_VERSION,
    )
    return EvidenceReport(**{**defaults, **overrides})


def _make_retrieval_result(**overrides: object) -> RetrievalResult:
    defaults: dict = dict(
        query="find related entities for Person:John",
        chunks_retrieved=(_HEX64,),
        rounds_used=1,
        budget_remaining=5000,
    )
    return RetrievalResult(**{**defaults, **overrides})


def _make_proposal_output(**overrides: object) -> AgentProposalOutput:
    defaults: dict = dict(
        candidate_id=_HEX64,
        proposal_class="canonicalize",
        rule_ids=("node:Person",),
        evidence_ids=(_HEX64,),
        rationale="Primary name needs canonical casing per schema rule.",
        confidence_score=0.9,
    )
    return AgentProposalOutput(**{**defaults, **overrides})


# ===========================================================================
# EvidenceItem
# ===========================================================================


class TestEvidenceItem:
    """Typed evidence fragment linked to a candidate."""

    def test_valid_construction(self) -> None:
        item = _make_evidence_item()
        assert item.chunk_id == _HEX64
        assert item.source_doc == "doc_001"
        assert item.classification == EvidenceClassification.supporting
        assert item.relevance_score == 0.85

    def test_frozen(self) -> None:
        item = _make_evidence_item()
        with pytest.raises(Exception):
            item.chunk_id = "changed"  # type: ignore[misc]

    # -- chunk_id validation --

    def test_chunk_id_must_be_64_chars(self) -> None:
        with pytest.raises(ValidationError, match="64-character"):
            _make_evidence_item(chunk_id="too_short")

    def test_chunk_id_63_chars_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_item(chunk_id="a" * 63)

    def test_chunk_id_65_chars_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_item(chunk_id="a" * 65)

    def test_chunk_id_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_item(chunk_id="")

    # -- source_doc validation --

    def test_source_doc_non_empty(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            _make_evidence_item(source_doc="")

    def test_source_doc_whitespace_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_item(source_doc="   ")

    # -- classification validation --

    def test_classification_supporting(self) -> None:
        item = _make_evidence_item(classification=EvidenceClassification.supporting)
        assert item.classification == "supporting"

    def test_classification_corroborating(self) -> None:
        item = _make_evidence_item(classification=EvidenceClassification.corroborating)
        assert item.classification == "corroborating"

    def test_classification_conflicting(self) -> None:
        item = _make_evidence_item(classification=EvidenceClassification.conflicting)
        assert item.classification == "conflicting"

    def test_all_classifications_accepted(self) -> None:
        """Every EvidenceClassification enum member is a valid construction value."""
        for cls in EvidenceClassification:
            item = _make_evidence_item(classification=cls)
            assert item.classification == cls

    def test_invalid_classification_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_item(classification="invalid_value")

    # -- relevance_score validation --

    def test_relevance_score_lower_bound(self) -> None:
        item = _make_evidence_item(relevance_score=0.0)
        assert item.relevance_score == 0.0

    def test_relevance_score_upper_bound(self) -> None:
        item = _make_evidence_item(relevance_score=1.0)
        assert item.relevance_score == 1.0

    def test_relevance_score_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_item(relevance_score=-0.01)

    def test_relevance_score_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_item(relevance_score=1.01)

    # -- page_locator (optional) --

    def test_page_locator_defaults_to_empty(self) -> None:
        item = EvidenceItem(
            chunk_id=_HEX64,
            source_doc="doc_001",
            classification=EvidenceClassification.supporting,
            relevance_score=0.5,
        )
        assert item.page_locator == ""


# ===========================================================================
# EvidenceReport (Agent-A output)
# ===========================================================================


class TestEvidenceReport:
    """Typed output of Agent-A containing classified evidence items."""

    def test_valid_construction(self) -> None:
        report = _make_evidence_report()
        assert report.candidate_id == _HEX64
        assert report.sufficient is True
        assert len(report.items) == 1
        assert report.run_id == _RUN_ID
        assert report.schema_version == _SCHEMA_VERSION

    def test_frozen(self) -> None:
        report = _make_evidence_report()
        with pytest.raises(Exception):
            report.sufficient = False  # type: ignore[misc]

    # -- candidate_id validation --

    def test_candidate_id_must_be_64_chars(self) -> None:
        with pytest.raises(ValidationError, match="64-character"):
            _make_evidence_report(candidate_id="short")

    def test_candidate_id_63_chars_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_report(candidate_id="a" * 63)

    # -- run_id / schema_version validation --

    def test_run_id_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_report(run_id="")

    def test_run_id_whitespace_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_report(run_id="   ")

    def test_schema_version_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_report(schema_version="")

    def test_schema_version_whitespace_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_report(schema_version="  \t ")

    # -- sufficiency_score bounds --

    def test_sufficiency_score_zero(self) -> None:
        report = _make_evidence_report(sufficiency_score=0.0, sufficient=False)
        assert report.sufficiency_score == 0.0

    def test_sufficiency_score_one(self) -> None:
        report = _make_evidence_report(sufficiency_score=1.0)
        assert report.sufficiency_score == 1.0

    def test_sufficiency_score_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_report(sufficiency_score=-0.1)

    def test_sufficiency_score_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_evidence_report(sufficiency_score=1.1)

    # -- items --

    def test_empty_items_allowed(self) -> None:
        """No items is valid — represents insufficient evidence fallback."""
        report = _make_evidence_report(items=(), sufficiency_score=0.0, sufficient=False)
        assert len(report.items) == 0

    def test_multiple_items(self) -> None:
        items = (
            _make_evidence_item(classification=EvidenceClassification.supporting),
            _make_evidence_item(
                chunk_id=_HEX64_ALT,
                classification=EvidenceClassification.conflicting,
            ),
        )
        report = _make_evidence_report(items=items)
        assert len(report.items) == 2
        assert report.items[0].classification == EvidenceClassification.supporting
        assert report.items[1].classification == EvidenceClassification.conflicting

    # -- idempotency --

    def test_idempotent_reconstruction(self) -> None:
        """model_dump → model_validate yields identical data."""
        original = _make_evidence_report()
        data = original.model_dump(mode="json")
        reconstructed = EvidenceReport.model_validate(data)
        assert reconstructed.candidate_id == original.candidate_id
        assert reconstructed.sufficiency_score == original.sufficiency_score
        assert reconstructed.sufficient == original.sufficient
        assert len(reconstructed.items) == len(original.items)

    # -- missing required fields --

    def test_missing_candidate_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceReport(
                items=(),
                sufficiency_score=0.0,
                sufficient=False,
                run_id=_RUN_ID,
                schema_version=_SCHEMA_VERSION,
            )

    def test_missing_run_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceReport(
                candidate_id=_HEX64,
                items=(),
                sufficiency_score=0.0,
                sufficient=False,
                schema_version=_SCHEMA_VERSION,
            )

    def test_missing_sufficient_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceReport(
                candidate_id=_HEX64,
                items=(),
                sufficiency_score=0.0,
                run_id=_RUN_ID,
                schema_version=_SCHEMA_VERSION,
            )


# ===========================================================================
# RetrievalResult (Agent-B output)
# ===========================================================================


class TestRetrievalResult:
    """Typed output of Agent-B recording a single retrieval round."""

    def test_valid_construction(self) -> None:
        result = _make_retrieval_result()
        assert result.query == "find related entities for Person:John"
        assert result.rounds_used == 1
        assert result.budget_remaining == 5000
        assert len(result.chunks_retrieved) == 1

    def test_frozen(self) -> None:
        result = _make_retrieval_result()
        with pytest.raises(Exception):
            result.query = "changed"  # type: ignore[misc]

    # -- query validation --

    def test_query_empty_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            _make_retrieval_result(query="")

    def test_query_whitespace_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_retrieval_result(query="   ")

    # -- rounds_used validation --

    def test_rounds_used_zero_accepted(self) -> None:
        result = _make_retrieval_result(rounds_used=0)
        assert result.rounds_used == 0

    def test_rounds_used_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_retrieval_result(rounds_used=-1)

    # -- budget_remaining validation --

    def test_budget_remaining_zero_accepted(self) -> None:
        result = _make_retrieval_result(budget_remaining=0)
        assert result.budget_remaining == 0

    def test_budget_remaining_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_retrieval_result(budget_remaining=-1)

    # -- chunks_retrieved --

    def test_empty_chunks_allowed(self) -> None:
        result = _make_retrieval_result(chunks_retrieved=())
        assert len(result.chunks_retrieved) == 0

    def test_multiple_chunks(self) -> None:
        result = _make_retrieval_result(chunks_retrieved=(_HEX64, _HEX64_ALT))
        assert len(result.chunks_retrieved) == 2

    # -- idempotency --

    def test_idempotent_reconstruction(self) -> None:
        original = _make_retrieval_result()
        data = original.model_dump(mode="json")
        reconstructed = RetrievalResult.model_validate(data)
        assert reconstructed.query == original.query
        assert reconstructed.rounds_used == original.rounds_used
        assert reconstructed.budget_remaining == original.budget_remaining

    # -- missing required fields --

    def test_missing_query_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalResult(
                chunks_retrieved=(),
                rounds_used=1,
                budget_remaining=5000,
            )

    def test_missing_rounds_used_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalResult(
                query="something",
                chunks_retrieved=(),
                budget_remaining=5000,
            )

    def test_missing_budget_remaining_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RetrievalResult(
                query="something",
                chunks_retrieved=(),
                rounds_used=1,
            )


# ===========================================================================
# AgentProposalOutput (Agent-P output)
# ===========================================================================


class TestAgentProposalOutput:
    """Typed output of Agent-P — the proposal contract before safety guard."""

    def test_valid_construction(self) -> None:
        output = _make_proposal_output()
        assert output.candidate_id == _HEX64
        assert output.proposal_class == "canonicalize"
        assert output.confidence_score == 0.9
        assert len(output.rule_ids) == 1
        assert len(output.evidence_ids) == 1

    def test_frozen(self) -> None:
        output = _make_proposal_output()
        with pytest.raises(Exception):
            output.rationale = "changed"  # type: ignore[misc]

    # -- candidate_id validation --

    def test_candidate_id_must_be_64_chars(self) -> None:
        with pytest.raises(ValidationError, match="64-character"):
            _make_proposal_output(candidate_id="short")

    def test_candidate_id_63_chars_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_proposal_output(candidate_id="a" * 63)

    def test_candidate_id_65_chars_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_proposal_output(candidate_id="a" * 65)

    # -- proposal_class validation --

    def test_all_valid_proposal_classes(self) -> None:
        """Every ProposalClass enum value is accepted."""
        for cls in ("canonicalize", "normalize", "rename", "merge", "delete", "defer"):
            output = _make_proposal_output(proposal_class=cls)
            assert output.proposal_class == cls

    def test_invalid_proposal_class_rejected(self) -> None:
        with pytest.raises(ValidationError, match="proposal_class"):
            _make_proposal_output(proposal_class="invalid_class")

    def test_empty_proposal_class_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_proposal_output(proposal_class="")

    # -- rationale validation --

    def test_rationale_non_empty(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            _make_proposal_output(rationale="")

    def test_rationale_whitespace_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_proposal_output(rationale="   ")

    # -- confidence_score bounds --

    def test_confidence_score_zero(self) -> None:
        output = _make_proposal_output(confidence_score=0.0)
        assert output.confidence_score == 0.0

    def test_confidence_score_one(self) -> None:
        output = _make_proposal_output(confidence_score=1.0)
        assert output.confidence_score == 1.0

    def test_confidence_score_below_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_proposal_output(confidence_score=-0.01)

    def test_confidence_score_above_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make_proposal_output(confidence_score=1.01)

    # -- optional tuple fields --

    def test_empty_rule_ids_allowed(self) -> None:
        output = _make_proposal_output(rule_ids=())
        assert len(output.rule_ids) == 0

    def test_empty_evidence_ids_allowed(self) -> None:
        output = _make_proposal_output(evidence_ids=())
        assert len(output.evidence_ids) == 0

    def test_multiple_rule_ids(self) -> None:
        output = _make_proposal_output(rule_ids=("node:Person", "edge:WORKS_FOR"))
        assert len(output.rule_ids) == 2

    # -- idempotency --

    def test_idempotent_reconstruction(self) -> None:
        """model_dump → model_validate yields identical data."""
        original = _make_proposal_output()
        data = original.model_dump(mode="json")
        reconstructed = AgentProposalOutput.model_validate(data)
        assert reconstructed.candidate_id == original.candidate_id
        assert reconstructed.proposal_class == original.proposal_class
        assert reconstructed.rationale == original.rationale
        assert reconstructed.confidence_score == original.confidence_score

    # -- missing required fields --

    def test_missing_candidate_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentProposalOutput(
                proposal_class="canonicalize",
                rationale="reason",
                confidence_score=0.9,
            )

    def test_missing_proposal_class_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentProposalOutput(
                candidate_id=_HEX64,
                rationale="reason",
                confidence_score=0.9,
            )

    def test_missing_rationale_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentProposalOutput(
                candidate_id=_HEX64,
                proposal_class="canonicalize",
                confidence_score=0.9,
            )

    def test_missing_confidence_score_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentProposalOutput(
                candidate_id=_HEX64,
                proposal_class="canonicalize",
                rationale="reason",
            )


# ===========================================================================
# Cross-model consistency
# ===========================================================================


class TestCrossModelConsistency:
    """Verify that evidence items nest cleanly inside evidence reports,
    and that all models share consistent validation semantics."""

    def test_report_with_all_classification_types(self) -> None:
        """EvidenceReport can hold items of every classification type."""
        items = tuple(
            _make_evidence_item(
                chunk_id=f"{'abcdef0123456789' * 4}",
                classification=cls,
            )
            for cls in EvidenceClassification
        )
        report = _make_evidence_report(items=items)
        classifications = {item.classification for item in report.items}
        assert classifications == set(EvidenceClassification)

    def test_proposal_candidate_id_matches_report(self) -> None:
        """AgentProposalOutput and EvidenceReport can share the same candidate_id."""
        shared_id = "c" * 64
        report = _make_evidence_report(candidate_id=shared_id)
        proposal = _make_proposal_output(candidate_id=shared_id)
        assert report.candidate_id == proposal.candidate_id

    def test_evidence_classification_enum_exhaustive(self) -> None:
        """EvidenceClassification has exactly three members."""
        assert set(EvidenceClassification) == {
            EvidenceClassification.supporting,
            EvidenceClassification.corroborating,
            EvidenceClassification.conflicting,
        }


# ===========================================================================
# Agent-P builder — high_risk_override propagation
# ===========================================================================


class TestBuilderHighRiskOverridePropagation:
    """Verify high_risk_override flows from AgentProposalOutput to ProposalPacket."""

    @staticmethod
    def _make_decision() -> "OrchestratorDecision":
        from api.agents.orchestrator import OrchestratorDecision, RiskClass, ToolBudget
        return OrchestratorDecision(
            run_id=_RUN_ID,
            schema_version=_SCHEMA_VERSION,
            candidate_id=_HEX64,
            risk_class=RiskClass.medium,
            budget=ToolBudget(
                max_input_tokens_a=4000, max_output_tokens_a=1000,
                max_input_tokens_b=4000, max_output_tokens_b=1000,
                max_input_tokens_p=4000, max_output_tokens_p=1000,
                max_retrieval_rounds=3, cost_limit=0.05, timeout_s=60.0,
            ),
            model_a="test/model-a",
            model_b="test/model-b",
            model_p="test/model-p",
        )

    @staticmethod
    def _make_candidate() -> "Candidate":
        from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity
        return Candidate(
            run_id=_RUN_ID,
            schema_version=_SCHEMA_VERSION,
            candidate_type=CandidateType.canonical_violation,
            candidate_lane=CandidateLane.relationship,
            involved_element_refs=("rel_a",),
            severity=Severity.high,
            detection_method="canonical_direction_violation",
            collision_context={"rel_type": "MENTIONS"},
        )

    def test_high_risk_override_propagates_true(self) -> None:
        """high_risk_override=True on AgentProposalOutput reaches ProposalPacket."""
        from api.agents.proposal import ProposalComposerAgent

        output = _make_proposal_output(
            proposal_class="rename", high_risk_override=True,
        )
        packet = ProposalComposerAgent._build_proposal_packet(
            output=output,
            candidate=self._make_candidate(),
            decision=self._make_decision(),
            evidence_report=_make_evidence_report(),
        )
        assert packet.high_risk_override is True
        assert packet.is_high_risk() is True

    def test_high_risk_override_propagates_false(self) -> None:
        """high_risk_override=False (default) also propagates correctly."""
        from api.agents.proposal import ProposalComposerAgent

        output = _make_proposal_output(proposal_class="normalize")
        packet = ProposalComposerAgent._build_proposal_packet(
            output=output,
            candidate=self._make_candidate(),
            decision=self._make_decision(),
            evidence_report=_make_evidence_report(),
        )
        assert packet.high_risk_override is False
        assert packet.is_high_risk() is False
