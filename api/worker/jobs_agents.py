"""
api/worker/jobs_agents.py — ARQ jobs: agent curation pipeline (SPEC-07).

Agent curation pipeline jobs (SPEC-07 S-07.7)
----------------------------------------------
Three chained jobs forming: evidence -> (optional retrieval) -> proposal.

  evidence_assembly_job(ctx, run_id, candidate_json, decision_json)
    Deserialise candidate + OrchestratorDecision, call Agent-A, store
    EvidenceReport.  If evidence insufficient -> enqueue retrieval job.
    If sufficient -> enqueue proposal job.

  retrieval_augmentation_job(ctx, run_id, candidate_json, decision_json,
                             evidence_report_json)
    Call Agent-B with the existing evidence.  Always enqueues proposal
    job afterward (Agent-P decides whether to defer).

  proposal_composition_job(ctx, run_id, candidate_json, decision_json,
                           evidence_report_json)
    Call Agent-P, create proposal via ProposalService.  Pipeline terminal.

Each agent job: fail-closed, telemetry recorded, budget checked before LLM
call.  Errors are caught, logged at ERROR, status marked "failed", job
returns without raising so sibling candidates are unaffected.

Job arguments
-------------
Agent pipeline: run_id (str), candidate_json (str), decision_json (str),
  evidence_report_json (str — omitted for evidence_assembly_job),
  correlation_id (str).

All jobs accept an optional ``correlation_id`` parameter (SKILL-D R-D2).
When non-empty, the ID is bound to structlog's context at job start so
every log entry within the job carries the same correlation ID as the
originating HTTP request.  Chained jobs propagate the ID to the next
``enqueue_job()`` call.

Serialisation: Pydantic model_dump_json() for complex args; deserialised via
model_validate_json() at the start of each job.

AgentPipelineJobStatus
----------------------
  Persisted to Redis under CacheKey.agent_job(run_id, candidate_id) with a
  24-hour TTL.  Read by the monitoring endpoint
  GET /api/monitoring/agents/{run_id} to surface per-candidate pipeline
  progress to the UI.

Sensitive data (SKILL-D R-D5)
------------------------------
  Only candidate_id (safe hashes) appear in log entries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.observability.correlation import set_correlation_id
from api.observability.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public: per-candidate agent pipeline status model
# ---------------------------------------------------------------------------


class AgentPipelineJobStatus(BaseModel):
    """Redis-persisted status for a per-candidate agent pipeline execution.

    Written by the three chained agent jobs at each lifecycle transition;
    read by api/routers/monitoring.py (GET /api/monitoring/agents/{run_id}).

    Attributes:
        run_id:              Governed run.
        candidate_id:        Candidate being processed (64-char SHA-256).
        stage:               Current pipeline stage.
        error:               Error message on failure; None otherwise.
        proposal_id:         Proposal ID created on success; None otherwise.
        evidence_items:      Evidence items gathered by Agent-A.
        evidence_sufficient: Whether evidence met the sufficiency threshold.
        retrieval_rounds:    Retrieval rounds executed by Agent-B (0 if skipped).
        started_at:          ISO 8601 UTC timestamp of pipeline start.
        updated_at:          ISO 8601 UTC timestamp of last status update.
    """

    run_id: str
    candidate_id: str
    stage: Literal[
        "queued",
        "evidence_running", "evidence_complete",
        "retrieval_running", "retrieval_complete",
        "proposal_running",
        "complete", "failed", "deferred",
    ]
    error: str | None = None
    proposal_id: str | None = None
    evidence_items: int = 0
    evidence_sufficient: bool = False
    retrieval_rounds: int = 0
    started_at: str | None = None
    updated_at: str | None = None


# ---------------------------------------------------------------------------
# Agent pipeline status helpers
# ---------------------------------------------------------------------------

_AGENT_JOB_STATUS_TTL_S: int = 86400  # 24 hours


async def _set_agent_job_status(status: AgentPipelineJobStatus) -> None:
    """Write AgentPipelineJobStatus to Redis (TTL 24 h).

    Best-effort: cache failure is logged at WARNING by CacheClient and
    does not affect pipeline correctness.
    """
    cache = get_cache_client()
    await cache.set(
        CacheKey.agent_job(run_id=status.run_id, candidate_id=status.candidate_id),
        status,
        ttl=_AGENT_JOB_STATUS_TTL_S,
    )


async def _get_agent_job_status(
    run_id: str, candidate_id: str,
) -> AgentPipelineJobStatus | None:
    """Read AgentPipelineJobStatus from Redis.  None on miss or error."""
    cache = get_cache_client()
    return await cache.get(
        CacheKey.agent_job(run_id=run_id, candidate_id=candidate_id),
        model=AgentPipelineJobStatus,
    )


# ---------------------------------------------------------------------------
# Budget guard
# ---------------------------------------------------------------------------


def _check_budget(budget_field: int, field_name: str, run_id: str, candidate_id: str) -> bool:
    """Return True if the budget field is positive.  Logs ERROR and returns
    False if the budget is exhausted (<=0), indicating the LLM call should
    be skipped.
    """
    if budget_field <= 0:
        logger.error(
            "agent_budget_exhausted",
            run_id=run_id,
            candidate_id=candidate_id,
            field=field_name,
            value=budget_field,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# evidence_assembly_job
# ---------------------------------------------------------------------------


async def evidence_assembly_job(
    ctx: dict,
    run_id: str,
    candidate_json: str,
    decision_json: str,
    correlation_id: str = "",
) -> None:
    """Load candidate, call Agent-A, store EvidenceReport, chain next job.

    Args:
        ctx:            ARQ worker context dict.
        run_id:         Governed run identifier.
        candidate_json: JSON-serialised Candidate (model_dump_json output).
        decision_json:  JSON-serialised OrchestratorDecision.
        correlation_id: Propagated from the originating HTTP request (SKILL-D R-D2).

    Chain logic:
        - evidence sufficient   -> enqueue proposal_composition_job
        - evidence insufficient -> enqueue retrieval_augmentation_job
    """
    from api.agents.evidence import EvidenceAssemblyAgent
    from api.agents.orchestrator import OrchestratorDecision
    from api.models.candidate import Candidate

    # Bind correlation ID to this worker task's structlog context (SKILL-D R-D2).
    if correlation_id:
        set_correlation_id(correlation_id)

    started_at = datetime.now(UTC).isoformat()

    # Deserialise inputs.
    candidate = Candidate.model_validate_json(candidate_json)
    decision = OrchestratorDecision.model_validate_json(decision_json)
    candidate_id = candidate.candidate_id

    # Idempotency: skip if pipeline already completed for this candidate.
    existing = await _get_agent_job_status(run_id, candidate_id)
    if existing is not None and existing.stage in ("complete", "deferred"):
        logger.info(
            "agent_job_skipped_already_complete",
            run_id=run_id,
            candidate_id=candidate_id,
            stage=existing.stage,
        )
        return

    await _set_agent_job_status(AgentPipelineJobStatus(
        run_id=run_id,
        candidate_id=candidate_id,
        stage="evidence_running",
        started_at=started_at,
        updated_at=started_at,
    ))
    logger.info(
        "evidence_assembly_job_start",
        run_id=run_id,
        candidate_id=candidate_id,
    )

    try:
        # Budget check before LLM call.
        if not _check_budget(
            decision.budget.max_output_tokens_a,
            "max_output_tokens_a", run_id, candidate_id,
        ):
            raise RuntimeError("Agent-A output token budget is zero")

        agent = EvidenceAssemblyAgent()
        report = await agent.run(candidate=candidate, decision=decision)

        updated_at = datetime.now(UTC).isoformat()
        await _set_agent_job_status(AgentPipelineJobStatus(
            run_id=run_id,
            candidate_id=candidate_id,
            stage="evidence_complete",
            evidence_items=len(report.items),
            evidence_sufficient=report.sufficient,
            started_at=started_at,
            updated_at=updated_at,
        ))

        logger.info(
            "evidence_assembly_job_complete",
            run_id=run_id,
            candidate_id=candidate_id,
            evidence_items=len(report.items),
            sufficient=report.sufficient,
        )

        # Chain: always go straight to proposal composition.
        # Agent-B (retrieval augmentation) is skipped — the structural
        # recommendation from collision_context is the primary input for
        # Agent-P.  Agent-B will be re-enabled when curation-panel
        # document ingestion allows users to add new evidence.
        redis = ctx["redis"]
        evidence_report_json = report.model_dump_json()

        await redis.enqueue_job(
            "proposal_composition_job",
            run_id=run_id,
            candidate_json=candidate_json,
            decision_json=decision_json,
            evidence_report_json=evidence_report_json,
            correlation_id=correlation_id,
        )
        logger.info(
            "agent_chain_enqueued",
            run_id=run_id,
            candidate_id=candidate_id,
            next_job="proposal_composition_job",
            retrieval_skipped=True,
        )

    except Exception as exc:
        error_msg = str(exc)
        updated_at = datetime.now(UTC).isoformat()
        await _set_agent_job_status(AgentPipelineJobStatus(
            run_id=run_id,
            candidate_id=candidate_id,
            stage="failed",
            error=error_msg,
            started_at=started_at,
            updated_at=updated_at,
        ))
        logger.error(
            "evidence_assembly_job_failed",
            run_id=run_id,
            candidate_id=candidate_id,
            error=error_msg,
        )


# ---------------------------------------------------------------------------
# retrieval_augmentation_job
# ---------------------------------------------------------------------------


async def retrieval_augmentation_job(
    ctx: dict,
    run_id: str,
    candidate_json: str,
    decision_json: str,
    evidence_report_json: str,
    correlation_id: str = "",
) -> None:
    """Call Agent-B to augment insufficient evidence, then chain to proposal.

    Only enqueued when Agent-A returned sufficient=False.  Agent-B runs
    loop-guarded retrieval rounds within budget.  Always enqueues
    proposal_composition_job afterward — Agent-P decides whether to defer.

    Args:
        ctx:                   ARQ worker context dict.
        run_id:                Governed run identifier.
        candidate_json:        JSON-serialised Candidate.
        decision_json:         JSON-serialised OrchestratorDecision.
        evidence_report_json:  JSON-serialised EvidenceReport from Agent-A.
        correlation_id:        Propagated from the originating HTTP request (SKILL-D R-D2).
    """
    from api.agents.models import EvidenceReport
    from api.agents.orchestrator import OrchestratorDecision
    from api.agents.retrieval import RetrievalAugmentationAgent
    from api.models.candidate import Candidate

    # Bind correlation ID to this worker task's structlog context (SKILL-D R-D2).
    if correlation_id:
        set_correlation_id(correlation_id)

    candidate = Candidate.model_validate_json(candidate_json)
    decision = OrchestratorDecision.model_validate_json(decision_json)
    evidence_report = EvidenceReport.model_validate_json(evidence_report_json)
    candidate_id = candidate.candidate_id

    await _set_agent_job_status(AgentPipelineJobStatus(
        run_id=run_id,
        candidate_id=candidate_id,
        stage="retrieval_running",
        evidence_items=len(evidence_report.items),
        evidence_sufficient=False,
        started_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    ))
    logger.info(
        "retrieval_augmentation_job_start",
        run_id=run_id,
        candidate_id=candidate_id,
        initial_items=len(evidence_report.items),
    )

    try:
        # Budget check before LLM call.
        if not _check_budget(
            decision.budget.max_output_tokens_b,
            "max_output_tokens_b", run_id, candidate_id,
        ):
            raise RuntimeError("Agent-B output token budget is zero")

        agent = RetrievalAugmentationAgent()
        updated_report, round_results = await agent.run(
            candidate=candidate,
            decision=decision,
            evidence_report=evidence_report,
        )

        updated_at = datetime.now(UTC).isoformat()
        await _set_agent_job_status(AgentPipelineJobStatus(
            run_id=run_id,
            candidate_id=candidate_id,
            stage="retrieval_complete",
            evidence_items=len(updated_report.items),
            evidence_sufficient=updated_report.sufficient,
            retrieval_rounds=len(round_results),
            updated_at=updated_at,
        ))

        logger.info(
            "retrieval_augmentation_job_complete",
            run_id=run_id,
            candidate_id=candidate_id,
            rounds=len(round_results),
            final_items=len(updated_report.items),
            sufficient=updated_report.sufficient,
        )

        # Always chain to proposal — Agent-P decides whether to defer.
        redis = ctx["redis"]
        updated_evidence_json = updated_report.model_dump_json()
        await redis.enqueue_job(
            "proposal_composition_job",
            run_id=run_id,
            candidate_json=candidate_json,
            decision_json=decision_json,
            evidence_report_json=updated_evidence_json,
            correlation_id=correlation_id,
        )
        logger.info(
            "agent_chain_enqueued",
            run_id=run_id,
            candidate_id=candidate_id,
            next_job="proposal_composition_job",
        )

    except Exception as exc:
        error_msg = str(exc)
        updated_at = datetime.now(UTC).isoformat()
        await _set_agent_job_status(AgentPipelineJobStatus(
            run_id=run_id,
            candidate_id=candidate_id,
            stage="failed",
            error=error_msg,
            updated_at=updated_at,
        ))
        logger.error(
            "retrieval_augmentation_job_failed",
            run_id=run_id,
            candidate_id=candidate_id,
            error=error_msg,
        )


# ---------------------------------------------------------------------------
# proposal_composition_job
# ---------------------------------------------------------------------------


async def proposal_composition_job(
    ctx: dict,
    run_id: str,
    candidate_json: str,
    decision_json: str,
    evidence_report_json: str,
    correlation_id: str = "",
) -> None:
    """Call Agent-P to compose a proposal, submit to governed pipeline.

    Pipeline terminal: no further jobs are enqueued.  The proposal enters
    the approval queue via ProposalService.create() — same governed path
    as manual proposals (CLAUDE.md S4.2).

    Args:
        ctx:                   ARQ worker context dict.
        run_id:                Governed run identifier.
        candidate_json:        JSON-serialised Candidate.
        decision_json:         JSON-serialised OrchestratorDecision.
        evidence_report_json:  JSON-serialised EvidenceReport (from Agent-A,
                               possibly augmented by Agent-B).
        correlation_id:        Propagated from the originating HTTP request (SKILL-D R-D2).
    """
    from api.agents.models import EvidenceReport
    from api.agents.orchestrator import OrchestratorDecision
    from api.agents.proposal import ProposalComposerAgent
    from api.models.candidate import Candidate

    # Bind correlation ID to this worker task's structlog context (SKILL-D R-D2).
    if correlation_id:
        set_correlation_id(correlation_id)

    candidate = Candidate.model_validate_json(candidate_json)
    decision = OrchestratorDecision.model_validate_json(decision_json)
    evidence_report = EvidenceReport.model_validate_json(evidence_report_json)
    candidate_id = candidate.candidate_id

    # Read existing status to preserve started_at and retrieval_rounds.
    existing_status = await _get_agent_job_status(run_id, candidate_id)
    started_at = (
        existing_status.started_at
        if existing_status is not None and existing_status.started_at
        else datetime.now(UTC).isoformat()
    )
    retrieval_rounds = (
        existing_status.retrieval_rounds
        if existing_status is not None
        else 0
    )

    await _set_agent_job_status(AgentPipelineJobStatus(
        run_id=run_id,
        candidate_id=candidate_id,
        stage="proposal_running",
        evidence_items=len(evidence_report.items),
        evidence_sufficient=evidence_report.sufficient,
        retrieval_rounds=retrieval_rounds,
        started_at=started_at,
        updated_at=datetime.now(UTC).isoformat(),
    ))
    logger.info(
        "proposal_composition_job_start",
        run_id=run_id,
        candidate_id=candidate_id,
        evidence_items=len(evidence_report.items),
        evidence_sufficient=evidence_report.sufficient,
    )

    try:
        # Budget check before LLM call.
        if not _check_budget(
            decision.budget.max_output_tokens_p,
            "max_output_tokens_p", run_id, candidate_id,
        ):
            raise RuntimeError("Agent-P output token budget is zero")

        agent = ProposalComposerAgent()
        packet = await agent.run(
            candidate=candidate,
            decision=decision,
            evidence_report=evidence_report,
        )

        updated_at = datetime.now(UTC).isoformat()

        if packet is None:
            # Agent-P failed (LLM error, safety violation, or storage error).
            # Mark as failed — human can review the candidate manually.
            await _set_agent_job_status(AgentPipelineJobStatus(
                run_id=run_id,
                candidate_id=candidate_id,
                stage="failed",
                error="Agent-P returned no proposal (LLM failure or safety guard)",
                evidence_items=len(evidence_report.items),
                evidence_sufficient=evidence_report.sufficient,
                retrieval_rounds=retrieval_rounds,
                started_at=started_at,
                updated_at=updated_at,
            ))
            logger.error(
                "proposal_composition_job_no_proposal",
                run_id=run_id,
                candidate_id=candidate_id,
            )
            return

        # Determine terminal stage based on proposal class.
        terminal_stage: str = "complete"
        if str(packet.proposal_class) == "defer":
            terminal_stage = "deferred"

        await _set_agent_job_status(AgentPipelineJobStatus(
            run_id=run_id,
            candidate_id=candidate_id,
            stage=terminal_stage,  # type: ignore[arg-type]
            proposal_id=packet.proposal_id,
            evidence_items=len(evidence_report.items),
            evidence_sufficient=evidence_report.sufficient,
            retrieval_rounds=retrieval_rounds,
            started_at=started_at,
            updated_at=updated_at,
        ))

        logger.info(
            "proposal_composition_job_complete",
            run_id=run_id,
            candidate_id=candidate_id,
            proposal_id=packet.proposal_id,
            proposal_class=str(packet.proposal_class),
            stage=terminal_stage,
        )

    except Exception as exc:
        error_msg = str(exc)
        updated_at = datetime.now(UTC).isoformat()
        await _set_agent_job_status(AgentPipelineJobStatus(
            run_id=run_id,
            candidate_id=candidate_id,
            stage="failed",
            error=error_msg,
            evidence_items=len(evidence_report.items),
            evidence_sufficient=evidence_report.sufficient,
            retrieval_rounds=retrieval_rounds,
            started_at=started_at,
            updated_at=updated_at,
        ))
        logger.error(
            "proposal_composition_job_failed",
            run_id=run_id,
            candidate_id=candidate_id,
            error=error_msg,
        )
