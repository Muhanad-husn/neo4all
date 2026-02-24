"""
api/worker/jobs.py — ARQ jobs: extraction + agent curation pipeline.

Extraction job (SPEC-04 S-04.1)
-------------------------------
extraction_job(ctx, run_id, chunk_id, doc_id)

Pipeline: idempotency check -> retrieve chunk -> LLM extraction -> Neo4j
write -> mark complete.  Per-chunk failure handling: persist "failed", do NOT
raise, sibling chunks continue.

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
Extraction: run_id (str), chunk_id (str), doc_id (str).
Agent pipeline: run_id (str), candidate_json (str), decision_json (str),
  evidence_report_json (str — omitted for evidence_assembly_job).

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
  chunk.text is passed to services but never logged.
  Only chunk_id / candidate_id (safe hashes) appear in log entries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.graph.client import Neo4jClient
from api.graph.writer import GraphWriter
from api.observability.logger import get_logger
from api.services.extraction import ExtractionError, get_extraction_service
from api.vector.indexer import get_vector_indexer

logger = get_logger(__name__)

_JOB_STATUS_TTL_S: int = 86400  # 24 hours
_CHUNK_TEXT_TTL_S: int = 1800   # 30 minutes per SKILL-D R-D8


# ---------------------------------------------------------------------------
# Public: per-chunk job status model
# ---------------------------------------------------------------------------


class ChunkJobStatus(BaseModel):
    """Redis-persisted status for a single chunk extraction job.

    Written by extraction_job at each lifecycle transition; read by
    api/routers/monitoring.py (GET /api/monitoring/jobs/{run_id}).

    Attributes:
        run_id:        Governed run this job belongs to.
        chunk_id:      Target chunk identifier (safe to log — never raw text).
        doc_id:        Parent document identifier.
        status:        Job lifecycle state.
        error:         Error message set on "failed"; None otherwise.
        nodes_created: Nodes created in Neo4j (populated on "complete").
        nodes_matched: Nodes matched (de-duped) in Neo4j.
        edges_created: Edges created in Neo4j.
        edges_matched: Edges matched (de-duped) in Neo4j.
        edges_skipped: Edges skipped due to absent endpoint nodes.
        started_at:    ISO 8601 UTC timestamp of job_start event.
        completed_at:  ISO 8601 UTC timestamp of completion or failure.
    """

    run_id: str
    chunk_id: str
    doc_id: str
    status: Literal["queued", "running", "complete", "failed"]
    error: str | None = None
    nodes_created: int = 0
    nodes_matched: int = 0
    edges_created: int = 0
    edges_matched: int = 0
    edges_skipped: int = 0
    started_at: str | None = None
    completed_at: str | None = None


# ---------------------------------------------------------------------------
# Private: chunk text cache wrapper
# ---------------------------------------------------------------------------


class _CachedChunkText(BaseModel):
    """Internal Pydantic wrapper so CacheClient can serialize a plain string.

    CacheClient.get/set requires a BaseModel subclass. This thin wrapper lets
    us store and retrieve chunk text under CacheKey.chunk(chunk_id) without
    extending CacheClient with raw-string methods.
    """

    text: str


# ---------------------------------------------------------------------------
# Private: job status helpers
# ---------------------------------------------------------------------------


async def _set_job_status(status: ChunkJobStatus) -> None:
    """Write ChunkJobStatus to Redis (TTL 24 h).

    Cache failure is logged at WARNING by CacheClient and ignored here —
    status persistence is best-effort; it does not affect job correctness.
    """
    cache = get_cache_client()
    await cache.set(
        CacheKey.job_status(run_id=status.run_id, chunk_id=status.chunk_id),
        status,
        ttl=_JOB_STATUS_TTL_S,
    )


async def _get_job_status(run_id: str, chunk_id: str) -> ChunkJobStatus | None:
    """Read ChunkJobStatus from Redis. Returns None on cache miss or error."""
    cache = get_cache_client()
    return await cache.get(
        CacheKey.job_status(run_id=run_id, chunk_id=chunk_id),
        model=ChunkJobStatus,
    )


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


async def extraction_job(
    ctx: dict,
    run_id: str,
    chunk_id: str,
    doc_id: str,
) -> None:
    """Extract entities from one chunk and write them to Neo4j.

    Args:
        ctx:      ARQ worker context dict. Must contain "neo4j_client"
                  (an open Neo4jClient injected by WorkerSettings.on_startup).
        run_id:   Governed run identifier. The locked schema for this run must
                  be in Redis (approved in Phase 1) before enqueuing jobs.
        chunk_id: Target chunk identifier (Chunk.chunk_id — SHA-256 hex).
        doc_id:   Parent document identifier; stored in ChunkJobStatus for
                  per-job monitoring visibility.

    Returns:
        None. Results are persisted to Neo4j; status is persisted to Redis.

    Raises:
        Nothing. All exceptions are caught, logged at ERROR, and translated
        to status="failed" so that sibling chunks in the run continue.

    Log events:
        job_start                      INFO  — run_id, chunk_id, doc_id
        job_skipped_already_complete   INFO  — run_id, chunk_id
        chunk_cache_hit                DEBUG — run_id, chunk_id
        chunk_not_found                ERROR — run_id, chunk_id, doc_id
        extraction_complete            INFO  — run_id, chunk_id, node_count, edge_count
        graph_write_complete           INFO  — run_id, chunk_id, all write counts
        job_failed                     ERROR — run_id, chunk_id, doc_id, error
    """
    started_at = datetime.now(UTC).isoformat()

    # ------------------------------------------------------------------
    # 1. Operational idempotency check
    # ------------------------------------------------------------------
    existing = await _get_job_status(run_id=run_id, chunk_id=chunk_id)
    if existing is not None and existing.status == "complete":
        logger.info(
            "job_skipped_already_complete",
            run_id=run_id,
            chunk_id=chunk_id,
        )
        return

    # ------------------------------------------------------------------
    # 2. Mark running
    # ------------------------------------------------------------------
    await _set_job_status(
        ChunkJobStatus(
            run_id=run_id,
            chunk_id=chunk_id,
            doc_id=doc_id,
            status="running",
            started_at=started_at,
        )
    )
    logger.info("job_start", run_id=run_id, chunk_id=chunk_id, doc_id=doc_id)

    try:
        # ------------------------------------------------------------------
        # 3. Retrieve chunk text — cache-aside (SKILL-D R-D8)
        # ------------------------------------------------------------------
        cache = get_cache_client()

        cached_text = await cache.get(CacheKey.chunk(chunk_id), model=_CachedChunkText)
        if cached_text is not None:
            chunk_text: str = cached_text.text
            logger.debug("chunk_cache_hit", run_id=run_id, chunk_id=chunk_id)
        else:
            # Cache miss — retrieve from Qdrant (evidence-only read path).
            indexer = get_vector_indexer()
            raw_text = await indexer.get_chunk_text(run_id=run_id, chunk_id=chunk_id)
            if raw_text is None:
                logger.error(
                    "chunk_not_found",
                    run_id=run_id,
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                )
                raise ExtractionError(
                    f"Chunk text not found for chunk_id='{chunk_id}' in run='{run_id}'. "
                    "Ensure the chunk was indexed in Qdrant before enqueuing extraction."
                )
            # Populate cache — chunks are immutable, 30-min TTL sufficient.
            await cache.set(
                CacheKey.chunk(chunk_id),
                _CachedChunkText(text=raw_text),
                ttl=_CHUNK_TEXT_TTL_S,
            )
            chunk_text = raw_text

        # ------------------------------------------------------------------
        # 4. LLM extraction (fail-closed: ExtractionError on schema mismatch
        #    or malformed LLM output — CLAUDE.md §4.4)
        # ------------------------------------------------------------------
        extraction_svc = get_extraction_service()
        result = await extraction_svc.extract_chunk(
            run_id=run_id,
            chunk_id=chunk_id,
            chunk_text=chunk_text,  # never logged (SKILL-D R-D5)
        )
        logger.info(
            "extraction_complete",
            run_id=run_id,
            chunk_id=chunk_id,
            node_count=len(result.nodes),
            edge_count=len(result.edges),
        )

        # ------------------------------------------------------------------
        # 5. Write to Neo4j via GraphWriter
        #    Structural idempotency: MERGE on node_dedupe_key / rel_dedupe_key
        #    guarantees no duplicate nodes or edges on re-run.
        # ------------------------------------------------------------------
        neo4j_client: Neo4jClient = ctx["neo4j_client"]
        writer = GraphWriter(neo4j_client)
        write_result = await writer.write_extraction_result(result)
        logger.info(
            "graph_write_complete",
            run_id=run_id,
            chunk_id=chunk_id,
            nodes_created=write_result.nodes_created,
            nodes_matched=write_result.nodes_matched,
            edges_created=write_result.edges_created,
            edges_matched=write_result.edges_matched,
            edges_skipped=write_result.edges_skipped,
        )

        # ------------------------------------------------------------------
        # 6. Mark complete
        # ------------------------------------------------------------------
        completed_at = datetime.now(UTC).isoformat()
        await _set_job_status(
            ChunkJobStatus(
                run_id=run_id,
                chunk_id=chunk_id,
                doc_id=doc_id,
                status="complete",
                nodes_created=write_result.nodes_created,
                nodes_matched=write_result.nodes_matched,
                edges_created=write_result.edges_created,
                edges_matched=write_result.edges_matched,
                edges_skipped=write_result.edges_skipped,
                started_at=started_at,
                completed_at=completed_at,
            )
        )

    except Exception as exc:
        # ------------------------------------------------------------------
        # Per-chunk failure: persist FAILED status, log ERROR, do NOT raise.
        # Sibling chunk jobs for the same run must continue unaffected.
        # ------------------------------------------------------------------
        error_msg = str(exc)
        completed_at = datetime.now(UTC).isoformat()
        await _set_job_status(
            ChunkJobStatus(
                run_id=run_id,
                chunk_id=chunk_id,
                doc_id=doc_id,
                status="failed",
                error=error_msg,
                started_at=started_at,
                completed_at=completed_at,
            )
        )
        logger.error(
            "job_failed",
            run_id=run_id,
            chunk_id=chunk_id,
            doc_id=doc_id,
            error=error_msg,
        )


# ===========================================================================
# SPEC-07 S-07.7: Agent curation pipeline jobs
# ===========================================================================

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
) -> None:
    """Load candidate, call Agent-A, store EvidenceReport, chain next job.

    Args:
        ctx:            ARQ worker context dict.
        run_id:         Governed run identifier.
        candidate_json: JSON-serialised Candidate (model_dump_json output).
        decision_json:  JSON-serialised OrchestratorDecision.

    Chain logic:
        - evidence sufficient   -> enqueue proposal_composition_job
        - evidence insufficient -> enqueue retrieval_augmentation_job
    """
    from api.agents.evidence import EvidenceAssemblyAgent
    from api.agents.orchestrator import OrchestratorDecision
    from api.models.candidate import Candidate

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

        # Chain: enqueue next job via the worker's Redis connection.
        redis = ctx["redis"]
        evidence_report_json = report.model_dump_json()

        if report.sufficient:
            await redis.enqueue_job(
                "proposal_composition_job",
                run_id=run_id,
                candidate_json=candidate_json,
                decision_json=decision_json,
                evidence_report_json=evidence_report_json,
            )
            logger.info(
                "agent_chain_enqueued",
                run_id=run_id,
                candidate_id=candidate_id,
                next_job="proposal_composition_job",
            )
        else:
            await redis.enqueue_job(
                "retrieval_augmentation_job",
                run_id=run_id,
                candidate_json=candidate_json,
                decision_json=decision_json,
                evidence_report_json=evidence_report_json,
            )
            logger.info(
                "agent_chain_enqueued",
                run_id=run_id,
                candidate_id=candidate_id,
                next_job="retrieval_augmentation_job",
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
    """
    from api.agents.models import EvidenceReport
    from api.agents.orchestrator import OrchestratorDecision
    from api.agents.retrieval import RetrievalAugmentationAgent
    from api.models.candidate import Candidate

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
    """
    from api.agents.models import EvidenceReport
    from api.agents.orchestrator import OrchestratorDecision
    from api.agents.proposal import ProposalComposerAgent
    from api.models.candidate import Candidate

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
