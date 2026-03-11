"""
api/agents/retrieval.py — Agent-B: Retrieval Augmentation (SPEC-07 S-07.3).

LLM agent triggered ONLY when Agent-A returns sufficient=False.  Performs
loop-guarded, budget-aware retrieval rounds to gather additional evidence.

Each round: LLM generates query → Qdrant retrieval → append chunks →
re-evaluate sufficiency.  Terminates on: threshold met, max rounds, budget
exhausted, empty query (voluntary), or LLM failure.

Chunk texts cached via CacheKey.chunk (30 min).  Fail-closed on malformed LLM.
"""

from __future__ import annotations

import json
import time
from typing import Any

from api.agents.evidence import (
    CHUNK_CACHE_TTL,
    CachedChunkText,
    load_prompt_template,
)
from api.agents.models import EvidenceItem, EvidenceReport, RetrievalResult
from api.agents.orchestrator import OrchestratorDecision
from api.cache.client import CacheClient, get_cache_client
from api.cache.keys import CacheKey
from api.models.candidate import Candidate
from api.observability.logger import get_logger
from api.observability.metrics import get_metrics
from api.services.llm import JobConfig, LLMClient
from api.vector.retriever import EvidenceChunk, EvidenceRetriever, get_evidence_retriever

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JOB_ID: str = "retrieval_augmentation"
_TEMPLATE_VERSION: str = "v1"
_RETRIEVAL_TOP_K: int = 10
_PER_ROUND_TOKEN_COST: int = 500  # conservative per-round budget deduction
_AGENT_NAME: str = "agent-b"
_COST_PER_1K_TOKENS: float = 0.001
_SUFFICIENCY_THRESHOLD: float = 0.6  # matches Agent-A prompt contract


# ---------------------------------------------------------------------------
# RetrievalAugmentationAgent (Agent-B)
# ---------------------------------------------------------------------------


class RetrievalAugmentationAgent:
    """LLM agent that performs targeted retrieval rounds to fill evidence gaps.

    ONLY triggered when Agent-A's EvidenceReport has sufficient=False.
    Loop-guarded by max_retrieval_rounds and token budget.
    """

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        retriever: EvidenceRetriever | None = None,
        cache: CacheClient | None = None,
    ) -> None:
        self._llm = llm_client or LLMClient()
        self._retriever = retriever
        self._cache = cache

    def _get_retriever(self) -> EvidenceRetriever:
        if self._retriever is None:
            self._retriever = get_evidence_retriever()
        return self._retriever

    def _get_cache(self) -> CacheClient:
        if self._cache is None:
            self._cache = get_cache_client()
        return self._cache

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        candidate: Candidate,
        decision: OrchestratorDecision,
        evidence_report: EvidenceReport,
    ) -> tuple[EvidenceReport, list[RetrievalResult]]:
        """Run loop-guarded retrieval rounds to augment insufficient evidence.

        Returns (updated EvidenceReport, list of RetrievalResult per round).
        """
        metrics = get_metrics()
        candidate_id = candidate.candidate_id
        max_rounds = decision.budget.max_retrieval_rounds
        budget_remaining = decision.budget.max_input_tokens_b

        all_items: list[EvidenceItem] = list(evidence_report.items)
        seen_chunk_ids: set[str] = {item.chunk_id for item in all_items}
        initial_seen_count = len(seen_chunk_ids)
        round_results: list[RetrievalResult] = []
        retrieval_exhausted = False

        logger.info(
            "retrieval_augmentation_start",
            candidate_id=candidate_id,
            run_id=decision.run_id,
            max_rounds=max_rounds,
            initial_items=len(all_items),
            budget_remaining=budget_remaining,
        )

        for round_num in range(1, max_rounds + 1):
            if budget_remaining <= 0:
                logger.info(
                    "retrieval_budget_exhausted",
                    candidate_id=candidate_id,
                    run_id=decision.run_id,
                    round=round_num,
                )
                break

            round_t0 = time.perf_counter()

            # 1. LLM generates targeted query
            retrieval_result = await self._generate_query(
                candidate=candidate,
                decision=decision,
                current_items=all_items,
                rounds_used=round_num - 1,
                max_rounds=max_rounds,
                budget_remaining=budget_remaining,
            )

            if retrieval_result is None:
                logger.error(
                    "retrieval_query_generation_failed",
                    candidate_id=candidate_id,
                    run_id=decision.run_id,
                    round=round_num,
                )
                break

            # Voluntary termination: empty query.
            if not retrieval_result.query.strip():
                logger.info(
                    "retrieval_voluntary_termination",
                    candidate_id=candidate_id,
                    run_id=decision.run_id,
                    round=round_num,
                )
                round_results.append(retrieval_result)
                break

            # 2. Execute query against Qdrant
            new_chunks = await self._execute_retrieval(
                run_id=decision.run_id, query=retrieval_result.query,
            )

            # Deduplicate and cache
            fresh_chunks = [
                c for c in new_chunks if c.chunk_id not in seen_chunk_ids
            ]
            for c in fresh_chunks:
                seen_chunk_ids.add(c.chunk_id)
            await self._cache_chunks(fresh_chunks)

            budget_remaining = max(0, budget_remaining - _PER_ROUND_TOKEN_COST)
            chunk_ids = tuple(c.chunk_id for c in fresh_chunks)

            final_result = RetrievalResult(
                query=retrieval_result.query,
                chunks_retrieved=chunk_ids,
                rounds_used=round_num,
                budget_remaining=budget_remaining,
            )
            round_results.append(final_result)

            # 3. Append new chunks as corroborating evidence
            for chunk in fresh_chunks:
                all_items.append(
                    EvidenceItem(
                        chunk_id=chunk.chunk_id,
                        source_doc=chunk.doc_id,
                        page_locator=chunk.start_page_locator,
                        classification="corroborating",
                        relevance_score=chunk.relevance_score,
                    )
                )

            round_duration_ms = round(
                (time.perf_counter() - round_t0) * 1000, 2,
            )

            # 4. Telemetry
            self._record_round_telemetry(
                metrics=metrics,
                run_id=decision.run_id,
                candidate_id=candidate_id,
                round_num=round_num,
                duration_ms=round_duration_ms,
                chunks_found=len(fresh_chunks),
            )

            logger.info(
                "retrieval_round_complete",
                candidate_id=candidate_id,
                run_id=decision.run_id,
                round=round_num,
                chunks_found=len(fresh_chunks),
                total_items=len(all_items),
                budget_remaining=budget_remaining,
                duration_ms=round_duration_ms,
            )

            # 5. Short-circuit: if round 1 found nothing new, the vector
            #    store has no additional material for this candidate.
            #    Skip remaining rounds.
            if round_num == 1 and len(fresh_chunks) == 0:
                retrieval_exhausted = True
                logger.info(
                    "retrieval_exhausted_early_exit",
                    candidate_id=candidate_id,
                    run_id=decision.run_id,
                    initial_evidence_items=initial_seen_count,
                )
                break

            # 6. Re-evaluate sufficiency
            if self._estimate_sufficiency(all_items, candidate) >= _SUFFICIENCY_THRESHOLD:
                logger.info(
                    "retrieval_sufficiency_reached",
                    candidate_id=candidate_id,
                    run_id=decision.run_id,
                    round=round_num,
                )
                break

        # If we ran all rounds and never found anything new, mark as
        # exhausted.  This covers both the case where Agent-A provided
        # pre-attached evidence (initial_seen_count > 0) AND the case
        # where Agent-A found nothing (initial_seen_count == 0).
        if (
            not retrieval_exhausted
            and len(seen_chunk_ids) == initial_seen_count
        ):
            retrieval_exhausted = True

        # Build final updated evidence report
        sufficiency = self._estimate_sufficiency(all_items, candidate)
        updated_report = EvidenceReport(
            candidate_id=candidate.candidate_id,
            items=tuple(all_items),
            sufficiency_score=sufficiency,
            sufficient=sufficiency >= _SUFFICIENCY_THRESHOLD,
            retrieval_exhausted=retrieval_exhausted,
            run_id=decision.run_id,
            schema_version=decision.schema_version,
        )

        logger.info(
            "retrieval_augmentation_complete",
            candidate_id=candidate_id,
            run_id=decision.run_id,
            rounds_executed=len(round_results),
            final_items=len(all_items),
            final_sufficiency=sufficiency,
            sufficient=updated_report.sufficient,
            retrieval_exhausted=retrieval_exhausted,
        )
        return updated_report, round_results

    # ------------------------------------------------------------------
    # LLM query generation
    # ------------------------------------------------------------------

    async def _generate_query(
        self,
        candidate: Candidate,
        decision: OrchestratorDecision,
        current_items: list[EvidenceItem],
        rounds_used: int,
        max_rounds: int,
        budget_remaining: int,
    ) -> RetrievalResult | None:
        """Ask the LLM for a targeted retrieval query.  None on failure."""
        template = load_prompt_template(_JOB_ID, _TEMPLATE_VERSION)
        system_prompt: str = template["system_prompt"]

        candidate_json = json.dumps(
            candidate.model_dump(mode="json"), indent=2, ensure_ascii=True,
        )
        current_evidence = json.dumps(
            {
                "items_count": len(current_items),
                "classifications": {
                    "supporting": sum(
                        1 for i in current_items if i.classification == "supporting"
                    ),
                    "corroborating": sum(
                        1 for i in current_items if i.classification == "corroborating"
                    ),
                    "conflicting": sum(
                        1 for i in current_items if i.classification == "conflicting"
                    ),
                },
                "evidence_gap": (
                    "No evidence gathered yet."
                    if not current_items
                    else "Additional evidence needed to reach sufficiency threshold."
                ),
            },
            indent=2,
            ensure_ascii=True,
        )

        user_message: str = template["user_template"].format(
            candidate_json=candidate_json,
            current_evidence=current_evidence,
            budget_remaining=budget_remaining,
            rounds_used=rounds_used,
            max_rounds=max_rounds,
        )

        job = JobConfig(
            job_id=_JOB_ID,
            model=decision.model_b,
            temperature=0.2,
            max_tokens=decision.budget.max_output_tokens_b,
            response_format={"type": "json_object"},
        )
        return await self._llm.call(
            job=job,
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=RetrievalResult,
            run_id=decision.run_id,
        )

    # ------------------------------------------------------------------
    # Qdrant retrieval
    # ------------------------------------------------------------------

    async def _execute_retrieval(
        self, run_id: str, query: str,
    ) -> list[EvidenceChunk]:
        """Execute semantic search against Qdrant.  Empty list on error."""
        retriever = self._get_retriever()
        try:
            return await retriever.by_query(
                run_id=run_id, query_text=query, top_k=_RETRIEVAL_TOP_K,
            )
        except Exception as exc:
            logger.error("retrieval_qdrant_error", run_id=run_id, error=str(exc))
            return []

    # ------------------------------------------------------------------
    # Chunk caching
    # ------------------------------------------------------------------

    async def _cache_chunks(self, chunks: list[EvidenceChunk]) -> None:
        """Cache newly retrieved chunk texts via CacheKey.chunk."""
        cache = self._get_cache()
        for chunk in chunks:
            await cache.set(
                CacheKey.chunk(chunk_id=chunk.chunk_id),
                CachedChunkText(chunk_id=chunk.chunk_id, text=chunk.text),
                ttl=CHUNK_CACHE_TTL,
            )

    # ------------------------------------------------------------------
    # Sufficiency estimation
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_sufficiency(
        items: list[EvidenceItem],
        candidate: Candidate | None = None,
    ) -> float:
        """Deterministic heuristic with structural baseline from collision_context.

        Structural baseline (from deterministic detectors — aligned with
        evidence_assembly/v3 prompt values):
          - jaro_winkler >= 0.95:              +0.50
          - jaro_winkler >= 0.90:              +0.40
          - jaro_winkler >= 0.85:              +0.30
          - context_jaccard > 0:               +min(context_jaccard, 0.2)
          - token_overlap >= 0.5:              +0.05

        Textual evidence layer:
          - supporting:    +0.3 per item
          - corroborating: +0.15 per item
          - conflicting:   +0.1 per item

        Result clamped to [0.0, 1.0].

        Fast estimate so Agent-B can decide whether to continue rounds
        without re-invoking Agent-A each time.
        """
        score = 0.0

        # Structural baseline from collision_context
        if candidate is not None:
            ctx = candidate.collision_context or {}
            jw = ctx.get("jaro_winkler", 0.0)
            tok = ctx.get("token_overlap", 0.0)
            cj = ctx.get("context_jaccard", 0.0)
            if jw >= 0.95:
                score += 0.50
            elif jw >= 0.90:
                score += 0.40
            elif jw >= 0.85:
                score += 0.30
            if cj > 0.0:
                score += min(cj, 0.2)
            if tok >= 0.5:
                score += 0.05

        # Textual evidence layer
        for item in items:
            if item.classification == "supporting":
                score += 0.3
            elif item.classification == "corroborating":
                score += 0.15
            elif item.classification == "conflicting":
                score += 0.1
        return min(1.0, score)

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    @staticmethod
    def _record_round_telemetry(
        metrics: Any,
        run_id: str,
        candidate_id: str,
        round_num: int,
        duration_ms: float,
        chunks_found: int,
    ) -> None:
        """Record per-round telemetry keyed by (run_id, candidate_id, agent)."""
        labels = {"run_id": run_id, "candidate_id": candidate_id, "agent": _AGENT_NAME}
        metrics.increment("agent_calls", **labels)
        metrics.observe("agent_duration_ms", duration_ms, **labels)
        metrics.observe("agent_retrieval_round", float(round_num), **labels)
        metrics.observe("agent_chunks_retrieved", float(chunks_found), **labels)
        metrics.observe(
            "agent_cost_estimate",
            (_PER_ROUND_TOKEN_COST / 1000.0) * _COST_PER_1K_TOKENS,
            **labels,
        )
