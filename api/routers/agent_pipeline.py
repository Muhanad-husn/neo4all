"""
api/routers/agent_pipeline.py — Agent pipeline trigger & config endpoints (SPEC-07 S-07.9).

Layer 3: AI-assisted curation pipeline.
  POST /agents/run              — Trigger the agent pipeline for candidates with
                                  optional per-agent model overrides.
  GET  /agents/config           — Return the current AgentConfig defaults.
  GET  /agents/status/{run_id}  — Return per-candidate pipeline progress.

Architecture (SKILL-B: thin routers)
--------------------------------------
Orchestration logic lives in api/agents/orchestrator.py.
Worker jobs live in api/worker/jobs_agents.py.
Route handlers coordinate calls, map errors to HTTP status codes, and
build response models.  No agent logic in this file.

Error handling
--------------
POST /agents/run:
  409 — no locked schema for this run_id.
  404 — no cached candidates (run detection first).
  503 — ARQ enqueue failure.
GET /agents/config:
  Always HTTP 200.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel

from collections import defaultdict

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.config import AgentConfig, AgentModelConfig, get_settings
from api.models.responses import BaseResponse, ErrorDetail
from api.observability.correlation import get_correlation_id
from api.observability.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["agent-pipeline"])


# ---------------------------------------------------------------------------
# Node-safe batch grouping
# ---------------------------------------------------------------------------


def _group_by_shared_refs(
    candidates: list,
    decisions: list,
    max_batch_size: int,
) -> list[tuple[list, list]]:
    """Group candidates that share involved_element_refs into the same batch.

    Ensures no node is touched by two different batches — candidates sharing
    any entity ref are always processed together so the LLM sees the full
    cluster and execution never produces conflicting mutations.

    Unrelated candidates (no shared refs) are packed together up to
    max_batch_size for efficiency.

    Returns a list of (candidates, decisions) tuples — one per batch.
    """
    # Build union-find over involved_element_refs.
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

    # Use candidate index as a node in the union-find, linked through shared refs.
    # ref_to_idx: maps each entity ref to the first candidate index that uses it.
    ref_to_idx: dict[str, int] = {}
    # Also union candidate indices together via a parallel namespace ("cand:N").
    for i, c in enumerate(candidates):
        cand_key = f"cand:{i}"
        find(cand_key)  # register
        for ref in c.involved_element_refs:
            if ref in ref_to_idx:
                # This ref was seen in another candidate — union them.
                union(cand_key, f"cand:{ref_to_idx[ref]}")
            else:
                ref_to_idx[ref] = i

    # Collect groups by root.
    groups: dict[str, list[int]] = defaultdict(list)
    for i in range(len(candidates)):
        root = find(f"cand:{i}")
        groups[root].append(i)

    # Sort groups deterministically: by first candidate index.
    sorted_groups = sorted(groups.values(), key=lambda g: g[0])

    # Pack groups into batches. Each ref-connected group stays together.
    # Unrelated groups can share a batch up to max_batch_size.
    batches: list[tuple[list, list]] = []
    current_cands: list = []
    current_decs: list = []

    for group_indices in sorted_groups:
        group_cands = [candidates[i] for i in group_indices]
        group_decs = [decisions[i] for i in group_indices]

        # If this group alone exceeds max_batch_size, it gets its own batch.
        if len(group_indices) > max_batch_size:
            # Flush current batch if non-empty.
            if current_cands:
                batches.append((current_cands, current_decs))
                current_cands, current_decs = [], []
            batches.append((group_cands, group_decs))
            continue

        # Would adding this group exceed the batch limit?
        if len(current_cands) + len(group_indices) > max_batch_size:
            # Flush current batch.
            batches.append((current_cands, current_decs))
            current_cands, current_decs = [], []

        current_cands.extend(group_cands)
        current_decs.extend(group_decs)

    # Flush remaining.
    if current_cands:
        batches.append((current_cands, current_decs))

    return batches


# ---------------------------------------------------------------------------
# Request / Response models (SKILL-A R-A1, R-A2)
# ---------------------------------------------------------------------------


class RunAgentPipelineRequest(BaseModel):
    """Request body for POST /agents/run.

    Attributes:
        run_id:        Governed run to process.
        candidate_ids: Specific candidate IDs to process.  When empty or None,
                       all cached candidates for the run are processed.
        model_a:       Optional OpenRouter model override for Agent-A.
        model_b:       Optional OpenRouter model override for Agent-B.
        model_p:       Optional OpenRouter model override for Agent-P.
    """

    run_id: str
    candidate_ids: list[str] | None = None
    model_a: str | None = None
    model_b: str | None = None
    model_p: str | None = None
    confirm_archive: bool = False


class RunAgentPipelineResponse(BaseResponse):
    """Response for POST /agents/run.

    Attributes:
        jobs_enqueued:  Number of batch jobs enqueued.
        batch_size:     Candidates per batch job.
        model_a:        Agent-A model used for this run.
        model_b:        Agent-B model used for this run.
        model_p:        Agent-P model used for this run.
    """

    jobs_enqueued: int = 0
    batch_size: int = 0
    model_a: str = ""
    model_b: str = ""
    model_p: str = ""
    existing_proposal_count: int = 0
    pending_archive: bool = False


class AgentModelConfigOut(BaseModel):
    """Serializable agent model config for API responses."""

    model: str
    max_input_tokens: int
    max_output_tokens: int


class AgentConfigResponse(BaseResponse):
    """Response for GET /agents/config.

    Attributes:
        agent_a:                  Agent-A model configuration.
        agent_b:                  Agent-B model configuration.
        agent_p:                  Agent-P model configuration.
        cost_limit_per_candidate: USD cost cap per candidate.
        max_retrieval_rounds:     Agent-B loop guard.
    """

    agent_a: AgentModelConfigOut = AgentModelConfigOut(
        model="", max_input_tokens=0, max_output_tokens=0
    )
    agent_b: AgentModelConfigOut = AgentModelConfigOut(
        model="", max_input_tokens=0, max_output_tokens=0
    )
    agent_p: AgentModelConfigOut = AgentModelConfigOut(
        model="", max_input_tokens=0, max_output_tokens=0
    )
    cost_limit_per_candidate: float = 0.0
    max_retrieval_rounds: int = 0


class PipelineJobOut(BaseModel):
    """Per-candidate pipeline status for API responses."""

    candidate_id: str
    stage: str
    error: str | None = None
    proposal_id: str | None = None
    evidence_items: int = 0
    evidence_sufficient: bool = False
    retrieval_rounds: int = 0
    started_at: str | None = None
    updated_at: str | None = None


class PipelineStatusResponse(BaseResponse):
    """Response for GET /agents/status/{run_id}.

    Attributes:
        jobs:  Per-candidate pipeline status entries.
        total: Total number of jobs found.
    """

    jobs: list[PipelineJobOut] = []
    total: int = 0


class AgentCancelRequest(BaseModel):
    """Request body for POST /agents/cancel."""

    run_id: str


class AgentCancelResponse(BaseResponse):
    """Response for POST /agents/cancel.

    Attributes:
        jobs_cancelled: Number of agent jobs whose stage was overwritten to "cancelled".
    """

    jobs_cancelled: int = 0


# ---------------------------------------------------------------------------
# POST /agents/cancel
# ---------------------------------------------------------------------------


@router.post(
    "/agents/cancel",
    response_model=AgentCancelResponse,
    summary="Cancel all running/queued agent pipeline jobs for a run",
)
async def cancel_agent_pipeline(
    request: AgentCancelRequest,
) -> AgentCancelResponse:
    """Cancel all non-terminal agent pipeline jobs for a run.

    Scans Redis for all agent job status keys matching the run, and overwrites
    any jobs in non-terminal stages with ``"cancelled"``.  Already-complete,
    failed, or deferred jobs are left untouched.

    Cancelled jobs allow the pipeline to be re-triggered with different models.
    """
    from datetime import UTC, datetime

    from api.worker.jobs_agents import AgentPipelineJobStatus

    run_id = request.run_id
    logger.info("agent_pipeline_cancel_requested", run_id=run_id)

    cache = get_cache_client()
    keys = await cache.scan_keys(CacheKey.agent_job_prefix(run_id))
    jobs_cancelled = 0
    now = datetime.now(UTC).isoformat()

    _terminal_stages = {"complete", "failed", "deferred", "cancelled"}

    for key in keys:
        job: AgentPipelineJobStatus | None = await cache.get(
            key, model=AgentPipelineJobStatus
        )
        if job is None:
            continue
        if job.stage not in _terminal_stages:
            cancelled_job = AgentPipelineJobStatus(
                run_id=job.run_id,
                candidate_id=job.candidate_id,
                stage="cancelled",
                started_at=job.started_at,
                updated_at=now,
            )
            await cache.set(key, cancelled_job, ttl=86400)
            jobs_cancelled += 1

    logger.info(
        "agent_pipeline_cancel_complete",
        run_id=run_id,
        jobs_cancelled=jobs_cancelled,
    )

    return AgentCancelResponse(
        run_id=run_id,
        status="success",
        jobs_cancelled=jobs_cancelled,
    )


# ---------------------------------------------------------------------------
# GET /agents/config
# ---------------------------------------------------------------------------


@router.get(
    "/agents/config",
    response_model=AgentConfigResponse,
    summary="Return current per-agent model configuration defaults",
)
async def get_agent_config() -> AgentConfigResponse:
    """Return the current AgentConfig from Settings.

    Always returns HTTP 200.  The UI uses this to pre-populate model
    selection fields.
    """
    settings = get_settings()
    cfg = settings.AGENT_CONFIG

    return AgentConfigResponse(
        run_id="",
        status="success",
        agent_a=AgentModelConfigOut(
            model=cfg.agent_a.model,
            max_input_tokens=cfg.agent_a.max_input_tokens,
            max_output_tokens=cfg.agent_a.max_output_tokens,
        ),
        agent_b=AgentModelConfigOut(
            model=cfg.agent_b.model,
            max_input_tokens=cfg.agent_b.max_input_tokens,
            max_output_tokens=cfg.agent_b.max_output_tokens,
        ),
        agent_p=AgentModelConfigOut(
            model=cfg.agent_p.model,
            max_input_tokens=cfg.agent_p.max_input_tokens,
            max_output_tokens=cfg.agent_p.max_output_tokens,
        ),
        cost_limit_per_candidate=cfg.cost_limit_per_candidate,
        max_retrieval_rounds=cfg.max_retrieval_rounds,
    )


# ---------------------------------------------------------------------------
# GET /agents/status/{run_id}
# ---------------------------------------------------------------------------


@router.get(
    "/agents/status/{run_id}",
    response_model=PipelineStatusResponse,
    summary="Return per-candidate agent pipeline status for a run",
)
async def get_pipeline_status(run_id: str) -> PipelineStatusResponse:
    """Return the pipeline status for all candidates in a run.

    Scans Redis for all ``agent_job:{run_id}:*`` keys directly, so it
    works correctly regardless of which stage(s) were used to generate
    candidates.  Does not depend on a single candidate cache key.

    Always returns HTTP 200.  Returns an empty list when no pipeline jobs
    exist for this run (fail-open per SKILL-D R-D13).
    """
    from api.worker.jobs_agents import AgentPipelineJobStatus

    cache = get_cache_client()

    # Scan all agent job status keys for this run (works across stages).
    keys = await cache.scan_keys(CacheKey.agent_job_prefix(run_id))

    if not keys:
        return PipelineStatusResponse(run_id=run_id, status="success")

    # Read pipeline status for each key.
    jobs: list[PipelineJobOut] = []
    for key in keys:
        status: AgentPipelineJobStatus | None = await cache.get(
            key, model=AgentPipelineJobStatus
        )
        if status is not None:
            jobs.append(PipelineJobOut(
                candidate_id=status.candidate_id,
                stage=status.stage,
                error=status.error,
                proposal_id=status.proposal_id,
                evidence_items=status.evidence_items,
                evidence_sufficient=status.evidence_sufficient,
                retrieval_rounds=status.retrieval_rounds,
                started_at=status.started_at,
                updated_at=status.updated_at,
            ))

    return PipelineStatusResponse(
        run_id=run_id,
        status="success",
        jobs=jobs,
        total=len(jobs),
    )


# ---------------------------------------------------------------------------
# POST /agents/run
# ---------------------------------------------------------------------------


@router.post(
    "/agents/run",
    response_model=RunAgentPipelineResponse,
    summary="Trigger the AI agent pipeline for candidates with optional model overrides",
    responses={
        409: {
            "model": RunAgentPipelineResponse,
            "description": "Schema not locked — approve Phase 1 first",
        },
        404: {
            "model": RunAgentPipelineResponse,
            "description": "No cached candidates — run detection first",
        },
        503: {
            "model": RunAgentPipelineResponse,
            "description": "ARQ enqueue failure",
        },
    },
)
async def run_agent_pipeline(
    request: RunAgentPipelineRequest,
    response: Response,
) -> RunAgentPipelineResponse:
    """Trigger the AI agent pipeline for all or selected candidates.

    Orchestrates candidates, applies optional per-agent model overrides,
    and enqueues ``evidence_assembly_job`` for each candidate via ARQ.

    **Flow**:
    1. Verify schema is locked.
    2. Load cached candidates (from POST /candidates/generate).
    3. Build AgentConfig with any model overrides.
    4. Run Orchestrator to produce OrchestratorDecisions.
    5. Enqueue evidence_assembly_job per candidate.

    **Errors**:
    - ``409 Conflict`` — no locked schema for this run_id.
    - ``404 Not Found`` — no cached candidates (run detection first).
    - ``503 Service Unavailable`` — ARQ/Redis enqueue failure.
    """
    import hashlib

    from api.agents.orchestrator import Orchestrator, OrchestratorError
    from api.common.arq_pool import get_arq_pool
    from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity
    from api.schema.models import SchemaVersion

    run_id = request.run_id
    logger.info(
        "agent_pipeline_run_requested",
        run_id=run_id,
        candidate_count=len(request.candidate_ids) if request.candidate_ids else "all",
        model_a_override=request.model_a or "(default)",
        model_b_override=request.model_b or "(default)",
        model_p_override=request.model_p or "(default)",
    )

    cache = get_cache_client()

    # 1. Verify schema is locked.
    schema: SchemaVersion | None = await cache.get(
        CacheKey.schema(run_id=run_id), model=SchemaVersion
    )
    if schema is None:
        logger.warning("agent_pipeline_schema_not_locked", run_id=run_id)
        response.status_code = 409
        return RunAgentPipelineResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="schema_not_locked",
                    message=(
                        f"No locked schema found for run '{run_id}'. "
                        "Complete Phase 1 (approve the domain schema) first."
                    ),
                )
            ],
        )

    # 1b. Check for existing proposals — require explicit confirmation to archive.
    from api.proposals.service import ProposalService

    proposal_svc = ProposalService()
    existing_proposals = await proposal_svc.list_for_run(run_id)

    if existing_proposals and not request.confirm_archive:
        logger.info(
            "agent_pipeline_pending_archive",
            run_id=run_id,
            existing_count=len(existing_proposals),
        )
        response.status_code = 409
        return RunAgentPipelineResponse(
            run_id=run_id,
            status="error",
            existing_proposal_count=len(existing_proposals),
            pending_archive=True,
            errors=[
                ErrorDetail(
                    code="pending_archive",
                    message=(
                        f"There are {len(existing_proposals)} existing proposal(s) "
                        "from a previous pipeline run. Set confirm_archive=true to "
                        "archive them and proceed."
                    ),
                )
            ],
        )

    if existing_proposals and request.confirm_archive:
        archived_count = await proposal_svc.archive_for_run(run_id)
        logger.info(
            "agent_pipeline_proposals_archived",
            run_id=run_id,
            archived_count=archived_count,
        )

    # 2. Load cached candidates — scan all stage cache keys.
    from pydantic import BaseModel as _BM

    class _CandidateListCache(_BM):
        candidates: list[dict[str, Any]]
        schema_version: str

    # Try all stage variants (None, 1, 2, 3) to collect candidates from
    # any stage that has been generated.
    raw_candidates: list[dict[str, Any]] = []
    for stage_val in (None, 1, 2, 3):
        stage_tag = f"_stage{stage_val}" if stage_val is not None else ""
        dhash_payload = f"{schema.version_hash}:all_detectors_v1{stage_tag}"
        dhash = hashlib.sha256(dhash_payload.encode("utf-8")).hexdigest()[:32]
        cache_key = CacheKey.candidates(run_id=run_id, detection_hash=dhash)

        cached: _CandidateListCache | None = await cache.get(
            cache_key, model=_CandidateListCache
        )
        if cached is not None and cached.candidates:
            raw_candidates.extend(cached.candidates)

    # Deduplicate by candidate_id across stages.
    seen_ids: set[str] = set()
    deduped_candidates: list[dict[str, Any]] = []
    for c in raw_candidates:
        cid = c.get("candidate_id", "")
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            deduped_candidates.append(c)
    raw_candidates = deduped_candidates

    if not raw_candidates:
        logger.warning("agent_pipeline_no_candidates", run_id=run_id)
        response.status_code = 404
        return RunAgentPipelineResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="no_candidates",
                    message=(
                        f"No cached candidates found for run '{run_id}'. "
                        "Run candidate detection first (POST /candidates/generate)."
                    ),
                )
            ],
        )

    # 2a. Filter out excluded candidates — user already dismissed these.
    from api.routers.candidates import load_excluded_ids

    excluded_ids = await load_excluded_ids(run_id)
    if excluded_ids:
        before = len(raw_candidates)
        raw_candidates = [
            c for c in raw_candidates
            if c.get("candidate_id") not in excluded_ids
        ]
        excluded_count = before - len(raw_candidates)
        if excluded_count:
            logger.info(
                "agent_pipeline_excluded_filtered",
                run_id=run_id,
                excluded_count=excluded_count,
                remaining=len(raw_candidates),
            )

    # 2b. Filter out candidates that already have a non-rejected proposal.
    #     Reuses proposal_svc from step 1b; re-fetches because archive may
    #     have cleared old proposals while manual ones could still exist.
    #
    #     Any executed proposal (including canonicalize) filters the candidate.
    #     The structural recommender now proposes merge directly for high-
    #     similarity duplicates, so the canonicalize→merge escalation path
    #     is no longer needed.  Rejected proposals are skipped (re-triable).
    existing_proposals_list = await proposal_svc.list_for_run(run_id)
    has_proposal_ids: set[str] = set()
    for p in existing_proposals_list:
        if str(p.state) in ("rejected",):
            continue
        has_proposal_ids.add(p.candidate_id)
    if has_proposal_ids:
        before = len(raw_candidates)
        raw_candidates = [
            c for c in raw_candidates
            if c.get("candidate_id") not in has_proposal_ids
        ]
        skipped_proposal_count = before - len(raw_candidates)
        if skipped_proposal_count:
            logger.info(
                "agent_pipeline_existing_proposals_filtered",
                run_id=run_id,
                skipped_count=skipped_proposal_count,
                remaining=len(raw_candidates),
            )

    # Filter to requested candidate IDs if specified.
    if request.candidate_ids:
        requested = set(request.candidate_ids)
        raw_candidates = [c for c in raw_candidates if c.get("candidate_id") in requested]

    if not raw_candidates:
        response.status_code = 404
        return RunAgentPipelineResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="no_matching_candidates",
                    message="None of the specified candidate_ids were found in the cache.",
                )
            ],
        )

    # Reconstruct Candidate models from cached dicts.
    candidates: list[Candidate] = []
    for c in raw_candidates:
        try:
            candidates.append(
                Candidate(
                    run_id=c["run_id"],
                    schema_version=c["schema_version"],
                    candidate_type=CandidateType(c["candidate_type"]),
                    candidate_lane=CandidateLane(c["candidate_lane"]),
                    involved_element_refs=tuple(c.get("involved_element_refs", [])),
                    severity=Severity(c["severity"]),
                    detection_method=c.get("detection_method", ""),
                    collision_context=c.get("collision_context", {}),
                )
            )
        except Exception as exc:
            logger.warning(
                "agent_pipeline_candidate_skip",
                candidate_id=c.get("candidate_id", "?"),
                error=str(exc),
            )

    if not candidates:
        response.status_code = 404
        return RunAgentPipelineResponse(
            run_id=run_id,
            status="error",
            errors=[
                ErrorDetail(
                    code="no_valid_candidates",
                    message="All candidates failed reconstruction from cache.",
                )
            ],
        )

    # 2b. Suppress pairwise candidates that belong to a chain group.
    #     The group candidate handles the entire chain atomically.
    suppressed_count = 0
    active_candidates: list[Candidate] = []
    for c in candidates:
        if c.collision_context.get("suppressed_by_group"):
            suppressed_count += 1
        else:
            active_candidates.append(c)

    if suppressed_count > 0:
        logger.info(
            "agent_pipeline_chain_suppressed",
            run_id=run_id,
            suppressed_count=suppressed_count,
            active_count=len(active_candidates),
        )
    candidates = active_candidates

    if not candidates:
        return RunAgentPipelineResponse(
            run_id=run_id,
            status="success",
            jobs_enqueued=0,
        )

    # 3. Build AgentConfig with optional model overrides.
    settings = get_settings()
    base_cfg = settings.AGENT_CONFIG

    agent_a_cfg = AgentModelConfig(
        model=request.model_a or base_cfg.agent_a.model,
        max_input_tokens=base_cfg.agent_a.max_input_tokens,
        max_output_tokens=base_cfg.agent_a.max_output_tokens,
    )
    agent_b_cfg = AgentModelConfig(
        model=request.model_b or base_cfg.agent_b.model,
        max_input_tokens=base_cfg.agent_b.max_input_tokens,
        max_output_tokens=base_cfg.agent_b.max_output_tokens,
    )
    agent_p_cfg = AgentModelConfig(
        model=request.model_p or base_cfg.agent_p.model,
        max_input_tokens=base_cfg.agent_p.max_input_tokens,
        max_output_tokens=base_cfg.agent_p.max_output_tokens,
    )

    agent_config = AgentConfig(
        agent_a=agent_a_cfg,
        agent_b=agent_b_cfg,
        agent_p=agent_p_cfg,
        cost_limit_per_candidate=base_cfg.cost_limit_per_candidate,
        max_retrieval_rounds=base_cfg.max_retrieval_rounds,
        reranking_top_n=base_cfg.reranking_top_n,
    )

    # 4. Orchestrate.
    try:
        orchestrator = Orchestrator(config=agent_config)
        decisions = orchestrator.orchestrate(candidates)
    except OrchestratorError as exc:
        logger.error("agent_pipeline_orchestration_error", run_id=run_id, error=str(exc))
        response.status_code = 503
        return RunAgentPipelineResponse(
            run_id=run_id,
            status="error",
            errors=[ErrorDetail(code="orchestration_error", message=str(exc))],
        )

    # 5. Clear prior pipeline status so the idempotency guard in
    #    evidence_assembly_job does not skip re-run candidates.
    for candidate in candidates:
        await cache.delete(
            CacheKey.agent_job(run_id=run_id, candidate_id=candidate.candidate_id)
        )

    # 6. Enqueue batch_evidence_assembly_job(s), grouped by batch_size.
    import json as _json

    correlation_id = get_correlation_id()
    batch_size = agent_config.batch_size

    try:
        arq_pool = await get_arq_pool()
    except Exception as exc:
        logger.error("agent_pipeline_arq_error", run_id=run_id, error=str(exc))
        response.status_code = 503
        return RunAgentPipelineResponse(
            run_id=run_id,
            status="error",
            errors=[ErrorDetail(code="arq_unavailable", message=str(exc))],
        )

    # ------------------------------------------------------------------
    # 6a. Group candidates by shared entity refs (node-safe batching).
    #     Candidates that share involved_element_refs MUST go in the same
    #     batch so the LLM sees the full cluster and execution never
    #     touches the same node twice.  Unrelated candidates are packed
    #     together up to batch_size for efficiency.
    # ------------------------------------------------------------------
    batches = _group_by_shared_refs(candidates, decisions, batch_size)

    logger.info(
        "agent_pipeline_batches_grouped",
        run_id=run_id,
        total_candidates=len(candidates),
        batches_formed=len(batches),
        batch_sizes=[len(b[0]) for b in batches],
    )

    enqueued = 0
    for batch_candidates, batch_decisions in batches:
        batch_cands_payload = _json.dumps(
            [c.model_dump(mode="json") for c in batch_candidates]
        )
        batch_decs_payload = _json.dumps(
            [d.model_dump(mode="json") for d in batch_decisions]
        )

        try:
            await arq_pool.enqueue_job(
                "batch_evidence_assembly_job",
                run_id=run_id,
                candidates_json=batch_cands_payload,
                decisions_json=batch_decs_payload,
                correlation_id=correlation_id,
            )
            enqueued += 1
        except Exception as exc:
            logger.error(
                "agent_pipeline_batch_enqueue_failed",
                run_id=run_id,
                batch_size=len(batch_candidates),
                error=str(exc),
            )

    logger.info(
        "agent_pipeline_run_complete",
        run_id=run_id,
        batch_jobs_enqueued=enqueued,
        total_candidates=len(candidates),
        batch_size=batch_size,
        model_a=agent_config.agent_a.model,
        model_b=agent_config.agent_b.model,
        model_p=agent_config.agent_p.model,
    )

    return RunAgentPipelineResponse(
        run_id=run_id,
        status="success",
        jobs_enqueued=enqueued,
        batch_size=batch_size,
        model_a=agent_config.agent_a.model,
        model_b=agent_config.agent_b.model,
        model_p=agent_config.agent_p.model,
    )
