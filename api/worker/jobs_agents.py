"""
api/worker/jobs_agents.py — ARQ jobs: agent curation pipeline (SPEC-07).

Agent curation pipeline jobs (SPEC-07 S-07.7)
----------------------------------------------
Two execution modes:

**Batch mode** (default):
  batch_evidence_assembly_job → batch_proposal_composition_job
  Processes multiple candidates in a single LLM call per agent, reducing
  API round-trips and sharing context (system prompts, schema rules).
  Agent-B candidates are split out to individual retrieval jobs.

**Single-candidate mode** (legacy/fallback):
  evidence_assembly_job → (optional retrieval) → proposal_composition_job
  One candidate per LLM call.  Used for Agent-B retrieval chaining.

Each agent job: fail-closed, telemetry recorded, budget checked before LLM
call.  Errors are caught, logged at ERROR, status marked "failed", job
returns without raising so sibling candidates are unaffected.

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
from api.worker.config_sync import sync_credentials_from_redis

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
        "complete", "failed", "deferred", "cancelled",
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
        - evidence sufficient                            -> enqueue proposal_composition_job
        - evidence insufficient + ENABLE_AGENT_B=True   -> enqueue retrieval_augmentation_job
        - evidence insufficient + ENABLE_AGENT_B=False  -> enqueue proposal_composition_job
    """
    from api.agents.evidence import EvidenceAssemblyAgent
    from api.agents.orchestrator import OrchestratorDecision
    from api.models.candidate import Candidate

    # Bind correlation ID to this worker task's structlog context (SKILL-D R-D2).
    if correlation_id:
        set_correlation_id(correlation_id)

    await sync_credentials_from_redis(ctx=ctx)

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

        # Chain: route to Agent-B or straight to proposal composition.
        # When ENABLE_AGENT_B is True and evidence is insufficient, the
        # pipeline routes through retrieval augmentation (Agent-B) to
        # search for complementary evidence before proposal composition.
        from api.config import get_settings

        settings = get_settings()
        redis = ctx["redis"]
        evidence_report_json = report.model_dump_json()

        agent_b_enabled = settings.ENABLE_AGENT_B
        route_to_agent_b = agent_b_enabled and not report.sufficient

        if route_to_agent_b:
            next_job = "retrieval_augmentation_job"
        else:
            next_job = "proposal_composition_job"

        await redis.enqueue_job(
            next_job,
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
            next_job=next_job,
            evidence_sufficient=report.sufficient,
            agent_b_enabled=agent_b_enabled,
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

    await sync_credentials_from_redis(ctx=ctx)

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

    await sync_credentials_from_redis(ctx=ctx)

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


# ---------------------------------------------------------------------------
# batch_evidence_assembly_job
# ---------------------------------------------------------------------------


async def batch_evidence_assembly_job(
    ctx: dict,
    run_id: str,
    candidates_json: str,
    decisions_json: str,
    correlation_id: str = "",
) -> None:
    """Process multiple candidates through Agent-A in a single LLM call.

    After batch evidence assembly, routes candidates:
      - Agent-B needed (insufficient + ENABLE_AGENT_B) -> individual retrieval jobs
      - Otherwise -> batch_proposal_composition_job

    Args:
        ctx:              ARQ worker context dict.
        run_id:           Governed run identifier.
        candidates_json:  JSON-serialised list of Candidate dicts.
        decisions_json:   JSON-serialised list of OrchestratorDecision dicts.
        correlation_id:   Propagated from the originating HTTP request.
    """
    import json as _json

    from api.agents.evidence import EvidenceAssemblyAgent
    from api.agents.orchestrator import OrchestratorDecision
    from api.models.candidate import Candidate

    if correlation_id:
        set_correlation_id(correlation_id)

    await sync_credentials_from_redis(ctx=ctx)

    started_at = datetime.now(UTC).isoformat()

    # Deserialise batch inputs.
    candidates_raw: list[dict] = _json.loads(candidates_json)
    decisions_raw: list[dict] = _json.loads(decisions_json)

    candidates = [Candidate.model_validate(c) for c in candidates_raw]
    decisions = [OrchestratorDecision.model_validate(d) for d in decisions_raw]

    # Set all candidates to evidence_running.
    for cand in candidates:
        existing = await _get_agent_job_status(run_id, cand.candidate_id)
        if existing is not None and existing.stage in ("complete", "deferred"):
            continue
        await _set_agent_job_status(AgentPipelineJobStatus(
            run_id=run_id,
            candidate_id=cand.candidate_id,
            stage="evidence_running",
            started_at=started_at,
            updated_at=started_at,
        ))

    logger.info(
        "batch_evidence_assembly_job_start",
        run_id=run_id,
        batch_size=len(candidates),
    )

    try:
        agent = EvidenceAssemblyAgent()
        reports = await agent.run_batch(candidates=candidates, decisions=decisions)

        updated_at = datetime.now(UTC).isoformat()

        # Update per-candidate status.
        for cand in candidates:
            cid = cand.candidate_id
            report = reports.get(cid)
            if report is None:
                continue
            await _set_agent_job_status(AgentPipelineJobStatus(
                run_id=run_id,
                candidate_id=cid,
                stage="evidence_complete",
                evidence_items=len(report.items),
                evidence_sufficient=report.sufficient,
                started_at=started_at,
                updated_at=updated_at,
            ))

        # Route: split Agent-B candidates from batch proposal candidates.
        from api.config import get_settings

        settings = get_settings()
        redis = ctx["redis"]
        agent_b_enabled = settings.ENABLE_AGENT_B

        proposal_candidates: list[dict] = []
        proposal_decisions: list[dict] = []
        proposal_evidence: list[dict] = []

        for cand, dec in zip(candidates, decisions):
            cid = cand.candidate_id
            report = reports.get(cid)
            if report is None:
                continue

            route_to_agent_b = agent_b_enabled and not report.sufficient

            if route_to_agent_b:
                # Enqueue individual retrieval job (Agent-B is loop-guarded).
                await redis.enqueue_job(
                    "retrieval_augmentation_job",
                    run_id=run_id,
                    candidate_json=cand.model_dump_json(),
                    decision_json=dec.model_dump_json(),
                    evidence_report_json=report.model_dump_json(),
                    correlation_id=correlation_id,
                )
                logger.info(
                    "batch_evidence_route_agent_b",
                    run_id=run_id,
                    candidate_id=cid,
                )
            else:
                proposal_candidates.append(cand.model_dump(mode="json"))
                proposal_decisions.append(dec.model_dump(mode="json"))
                proposal_evidence.append(report.model_dump(mode="json"))

        # Enqueue batch proposal job for non-Agent-B candidates.
        if proposal_candidates:
            await redis.enqueue_job(
                "batch_proposal_composition_job",
                run_id=run_id,
                candidates_json=_json.dumps(proposal_candidates),
                decisions_json=_json.dumps(proposal_decisions),
                evidence_reports_json=_json.dumps(proposal_evidence),
                correlation_id=correlation_id,
            )
            logger.info(
                "batch_evidence_chain_proposal",
                run_id=run_id,
                proposal_batch_size=len(proposal_candidates),
            )

    except Exception as exc:
        error_msg = str(exc)
        updated_at = datetime.now(UTC).isoformat()
        for cand in candidates:
            await _set_agent_job_status(AgentPipelineJobStatus(
                run_id=run_id,
                candidate_id=cand.candidate_id,
                stage="failed",
                error=error_msg,
                started_at=started_at,
                updated_at=updated_at,
            ))
        logger.error(
            "batch_evidence_assembly_job_failed",
            run_id=run_id,
            batch_size=len(candidates),
            error=error_msg,
        )


# ---------------------------------------------------------------------------
# Cluster consolidation for N-way merge
# ---------------------------------------------------------------------------


def _consolidate_clusters(
    candidates: list,
    decisions: list,
    evidence_reports: list,
) -> tuple[tuple[list, list, list], dict[str, list[str]]]:
    """Consolidate overlapping pairwise candidates into cluster candidates.

    Candidates that share involved_element_refs are merged into a single
    synthetic cluster candidate with ALL unique node refs.  This ensures
    Agent-P proposes ONE N-way merge instead of N conflicting pairwise merges.

    Returns:
        (consolidated_candidates, consolidated_decisions, consolidated_evidence):
            The reduced lists for Agent-P.
        cluster_map: dict mapping consolidated candidate_id -> list of original
            candidate_ids it represents.
    """
    from collections import defaultdict

    from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity

    n = len(candidates)
    if n == 0:
        return ([], [], []), {}

    # --- Union-find over candidate indices via shared refs ---
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        if x not in parent:
            parent[x] = x
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    ref_to_idx: dict[str, int] = {}
    for i, c in enumerate(candidates):
        cand_key = f"c:{i}"
        find(cand_key)
        for ref in c.involved_element_refs:
            if ref in ref_to_idx:
                union(cand_key, f"c:{ref_to_idx[ref]}")
            else:
                ref_to_idx[ref] = i

    # Group by root.
    groups: dict[str, list[int]] = defaultdict(list)
    for i in range(n):
        root = find(f"c:{i}")
        groups[root].append(i)

    sorted_groups = sorted(groups.values(), key=lambda g: g[0])

    cons_candidates: list = []
    cons_decisions: list = []
    cons_evidence: list = []
    cluster_map: dict[str, list[str]] = {}

    for group_indices in sorted_groups:
        if len(group_indices) == 1:
            # Singleton — pass through unchanged.
            idx = group_indices[0]
            cid = candidates[idx].candidate_id
            cons_candidates.append(candidates[idx])
            cons_decisions.append(decisions[idx])
            cons_evidence.append(evidence_reports[idx])
            cluster_map[cid] = [cid]
            continue

        # --- Build cluster candidate from multiple pairwise candidates ---
        group_cands = [candidates[i] for i in group_indices]
        group_decs = [decisions[i] for i in group_indices]
        group_evs = [evidence_reports[i] for i in group_indices]

        # Collect all unique node refs across the cluster.
        all_refs: set[str] = set()
        for c in group_cands:
            all_refs.update(c.involved_element_refs)

        # Pick survivor: highest-degree node from collision_context,
        # fallback to lexicographic first.
        degree_map: dict[str, int] = {}
        for c in group_cands:
            ctx = c.collision_context
            survivor_key = ctx.get("survivor_key")
            survivor_deg = ctx.get("survivor_degree", 0)
            if survivor_key:
                degree_map[survivor_key] = max(
                    degree_map.get(survivor_key, 0), survivor_deg
                )

        survivor = max(
            sorted(all_refs),
            key=lambda k: degree_map.get(k, 0),
        ) if degree_map else sorted(all_refs)[0]

        ordered_refs = (survivor,) + tuple(sorted(all_refs - {survivor}))

        # Use first candidate as template for run_id / schema_version.
        first = group_cands[0]

        # Build synthetic cluster candidate.
        cluster_cand = Candidate(
            run_id=first.run_id,
            schema_version=first.schema_version,
            candidate_type=CandidateType.probable_duplicate,
            candidate_lane=CandidateLane.node,
            involved_element_refs=ordered_refs,
            severity=Severity.high,
            detection_method="cluster_merge",
            collision_context={
                "cluster_size": len(all_refs),
                "pairwise_count": len(group_cands),
                "survivor_key": survivor,
                "survivor_degree": degree_map.get(survivor, 0),
                "component_members": list(sorted(all_refs)),
                "original_candidate_ids": [c.candidate_id for c in group_cands],
            },
        )

        # Use the decision from the first candidate (model config is shared).
        cluster_dec = group_decs[0]

        # Merge evidence: combine items from all evidence reports,
        # use the highest sufficiency score.
        all_items = []
        best_score = 0.0
        best_ev = group_evs[0]
        for ev in group_evs:
            all_items.extend(ev.items)
            if ev.sufficiency_score > best_score:
                best_score = ev.sufficiency_score
                best_ev = ev

        from api.agents.models import EvidenceReport

        cluster_ev = EvidenceReport(
            candidate_id=cluster_cand.candidate_id,
            items=tuple(all_items),
            sufficiency_score=best_score,
            sufficient=best_ev.sufficient,
            structural_recommendation=best_ev.structural_recommendation,
            linked_candidates=tuple(c.candidate_id for c in group_cands),
            run_id=first.run_id,
            schema_version=first.schema_version,
        )

        cons_candidates.append(cluster_cand)
        cons_decisions.append(cluster_dec)
        cons_evidence.append(cluster_ev)

        # Map cluster candidate_id → all original candidate_ids.
        cluster_map[cluster_cand.candidate_id] = [
            c.candidate_id for c in group_cands
        ]

    return (cons_candidates, cons_decisions, cons_evidence), cluster_map


# ---------------------------------------------------------------------------
# batch_proposal_composition_job
# ---------------------------------------------------------------------------


async def batch_proposal_composition_job(
    ctx: dict,
    run_id: str,
    candidates_json: str,
    decisions_json: str,
    evidence_reports_json: str,
    correlation_id: str = "",
) -> None:
    """Process multiple candidates through Agent-P in a single LLM call.

    Pipeline terminal: no further jobs are enqueued.  Each proposal enters
    the approval queue via ProposalService.create().

    Args:
        ctx:                    ARQ worker context dict.
        run_id:                 Governed run identifier.
        candidates_json:        JSON-serialised list of Candidate dicts.
        decisions_json:         JSON-serialised list of OrchestratorDecision dicts.
        evidence_reports_json:  JSON-serialised list of EvidenceReport dicts.
        correlation_id:         Propagated from the originating HTTP request.
    """
    import json as _json

    from api.agents.models import EvidenceReport
    from api.agents.orchestrator import OrchestratorDecision
    from api.agents.proposal import ProposalComposerAgent
    from api.models.candidate import Candidate

    if correlation_id:
        set_correlation_id(correlation_id)

    await sync_credentials_from_redis(ctx=ctx)

    candidates_raw: list[dict] = _json.loads(candidates_json)
    decisions_raw: list[dict] = _json.loads(decisions_json)
    evidence_raw: list[dict] = _json.loads(evidence_reports_json)

    candidates = [Candidate.model_validate(c) for c in candidates_raw]
    decisions = [OrchestratorDecision.model_validate(d) for d in decisions_raw]
    evidence_reports = [EvidenceReport.model_validate(e) for e in evidence_raw]

    # Preserve started_at from earlier stages.
    started_ats: dict[str, str] = {}
    retrieval_rounds_map: dict[str, int] = {}
    for cand in candidates:
        cid = cand.candidate_id
        existing = await _get_agent_job_status(run_id, cid)
        started_ats[cid] = (
            existing.started_at
            if existing is not None and existing.started_at
            else datetime.now(UTC).isoformat()
        )
        retrieval_rounds_map[cid] = (
            existing.retrieval_rounds if existing is not None else 0
        )

    # Set all to proposal_running.
    for cand, ev in zip(candidates, evidence_reports):
        cid = cand.candidate_id
        await _set_agent_job_status(AgentPipelineJobStatus(
            run_id=run_id,
            candidate_id=cid,
            stage="proposal_running",
            evidence_items=len(ev.items),
            evidence_sufficient=ev.sufficient,
            retrieval_rounds=retrieval_rounds_map.get(cid, 0),
            started_at=started_ats.get(cid, datetime.now(UTC).isoformat()),
            updated_at=datetime.now(UTC).isoformat(),
        ))

    # ------------------------------------------------------------------
    # Consolidate overlapping pairwise candidates into cluster candidates.
    # Candidates sharing entity refs become ONE cluster candidate with all
    # unique refs (survivor first).  Agent-P proposes ONE N-way merge per
    # cluster.  The result is mapped back to all original candidate_ids.
    # ------------------------------------------------------------------
    consolidated, cluster_map = _consolidate_clusters(
        candidates, decisions, evidence_reports
    )
    cons_candidates, cons_decisions, cons_evidence = consolidated

    logger.info(
        "batch_proposal_composition_job_start",
        run_id=run_id,
        original_count=len(candidates),
        consolidated_count=len(cons_candidates),
        clusters=[len(v) for v in cluster_map.values() if len(v) > 1],
    )

    try:
        agent = ProposalComposerAgent()
        results = await agent.run_batch(
            candidates=cons_candidates,
            decisions=cons_decisions,
            evidence_reports=cons_evidence,
        )

        # Expand cluster results back to all original candidate_ids.
        expanded_results: dict[str, Any] = {}
        for cons_cid, packet in results.items():
            if packet is None:
                continue
            # Map to all original candidate_ids in this cluster.
            original_cids = cluster_map.get(cons_cid, [cons_cid])
            for orig_cid in original_cids:
                expanded_results[orig_cid] = packet
        results = expanded_results

        updated_at = datetime.now(UTC).isoformat()

        for cand, ev in zip(candidates, evidence_reports):
            cid = cand.candidate_id
            packet = results.get(cid)

            if packet is None:
                await _set_agent_job_status(AgentPipelineJobStatus(
                    run_id=run_id,
                    candidate_id=cid,
                    stage="failed",
                    error="Agent-P returned no proposal (batch mode)",
                    evidence_items=len(ev.items),
                    evidence_sufficient=ev.sufficient,
                    retrieval_rounds=retrieval_rounds_map.get(cid, 0),
                    started_at=started_ats.get(cid, ""),
                    updated_at=updated_at,
                ))
                continue

            terminal_stage: str = "complete"
            if str(packet.proposal_class) == "defer":
                terminal_stage = "deferred"

            await _set_agent_job_status(AgentPipelineJobStatus(
                run_id=run_id,
                candidate_id=cid,
                stage=terminal_stage,  # type: ignore[arg-type]
                proposal_id=packet.proposal_id,
                evidence_items=len(ev.items),
                evidence_sufficient=ev.sufficient,
                retrieval_rounds=retrieval_rounds_map.get(cid, 0),
                started_at=started_ats.get(cid, ""),
                updated_at=updated_at,
            ))

            logger.info(
                "batch_proposal_composition_candidate_complete",
                run_id=run_id,
                candidate_id=cid,
                proposal_id=packet.proposal_id,
                proposal_class=str(packet.proposal_class),
            )

        logger.info(
            "batch_proposal_composition_job_complete",
            run_id=run_id,
            batch_size=len(candidates),
            success_count=sum(1 for v in results.values() if v is not None),
        )

    except Exception as exc:
        error_msg = str(exc)
        updated_at = datetime.now(UTC).isoformat()
        for cand, ev in zip(candidates, evidence_reports):
            cid = cand.candidate_id
            await _set_agent_job_status(AgentPipelineJobStatus(
                run_id=run_id,
                candidate_id=cid,
                stage="failed",
                error=error_msg,
                evidence_items=len(ev.items),
                evidence_sufficient=ev.sufficient,
                retrieval_rounds=retrieval_rounds_map.get(cid, 0),
                started_at=started_ats.get(cid, ""),
                updated_at=updated_at,
            ))
        logger.error(
            "batch_proposal_composition_job_failed",
            run_id=run_id,
            batch_size=len(candidates),
            error=error_msg,
        )
