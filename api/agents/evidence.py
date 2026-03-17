"""
api/agents/evidence.py — Agent-A: Evidence Assembly (SPEC-07 S-07.2).

LLM agent that classifies evidence for a candidate.  Receives candidate +
graph context, retrieves chunks from Qdrant, sends to LLM for classification,
returns typed EvidenceReport.  Does NOT decide actions.

Caching: graph context via GraphReader/CacheKey.graph_query (5 min),
chunk text via CacheKey.chunk (30 min).  Fail-closed on malformed LLM output.
"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, ConfigDict

from api.agents.models import EvidenceReport
from api.agents.orchestrator import OrchestratorDecision
from api.cache.client import CacheClient, get_cache_client
from api.cache.keys import CacheKey
from api.common.prompts import load_prompt_template
from api.graph.reader import GraphReader, get_graph_reader
from api.graph.reader_models import NeighborResult
from api.models.candidate import Candidate
from api.observability.logger import get_logger
from api.observability.metrics import get_metrics
from api.services.llm import JobConfig, LLMClient
from api.vector.retriever import EvidenceChunk, EvidenceRetriever, get_evidence_retriever

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JOB_ID: str = "evidence_assembly"
_TEMPLATE_VERSION: str = "v4"
CHUNK_CACHE_TTL: int = 1800  # 30 min (SKILL-D R-D8)
_MAX_CHUNKS_PER_REF: int = 10
_CHUNK_DELIMITER: str = "\n---CHUNK---\n"
_AGENT_NAME: str = "agent-a"
_COST_PER_1K_TOKENS: float = 0.001

_BATCH_JOB_ID: str = "evidence_assembly"
_BATCH_TEMPLATE_VERSION: str = "v6"


class CachedChunkText(BaseModel):
    """Pydantic wrapper for caching chunk text in Redis (SKILL-A R-A3)."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    text: str


# ---------------------------------------------------------------------------
# EvidenceAssemblyAgent (Agent-A)
# ---------------------------------------------------------------------------


class EvidenceAssemblyAgent:
    """LLM agent that assembles and classifies evidence for a candidate.

    Does NOT decide actions — only gathers evidence, classifies via LLM,
    and returns a typed EvidenceReport.  All dependencies are injectable.
    """

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        graph_reader: GraphReader | None = None,
        retriever: EvidenceRetriever | None = None,
        cache: CacheClient | None = None,
    ) -> None:
        self._llm = llm_client or LLMClient()
        self._graph_reader = graph_reader
        self._retriever = retriever
        self._cache = cache

    def _get_graph_reader(self) -> GraphReader:
        if self._graph_reader is None:
            self._graph_reader = get_graph_reader()
        return self._graph_reader

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
    ) -> EvidenceReport:
        """Assemble and classify evidence for a single candidate.

        1. Gather graph context (neighbours of each involved element).
        2. Retrieve chunk texts from Qdrant (cached via CacheKey.chunk).
        3. Build LLM prompt from evidence_assembly template.
        4. Call LLM and validate response as EvidenceReport.
        5. Record telemetry.  On failure → fallback with sufficient=False.
        """
        t0 = time.perf_counter()
        metrics = get_metrics()
        candidate_id = candidate.candidate_id

        logger.info(
            "evidence_assembly_start",
            candidate_id=candidate_id,
            candidate_type=str(candidate.candidate_type),
            run_id=decision.run_id,
        )

        # 1. Graph context
        graph_context = await self._gather_graph_context(
            run_id=decision.run_id,
            involved_refs=list(candidate.involved_element_refs),
        )

        # 2. Chunk texts
        chunks = await self._retrieve_chunks(
            run_id=decision.run_id,
            involved_refs=list(candidate.involved_element_refs),
        )

        # 3. Build prompt
        template = load_prompt_template(_JOB_ID, _TEMPLATE_VERSION)
        system_prompt: str = template["system_prompt"]
        candidate_json = json.dumps(
            candidate.model_dump(mode="json"), indent=2, ensure_ascii=True,
        )
        graph_context_json = json.dumps(graph_context, indent=2, ensure_ascii=True)
        chunk_texts = self._format_chunk_texts(chunks)
        user_message: str = template["user_template"].format(
            candidate_json=candidate_json,
            graph_context=graph_context_json,
            chunk_texts=chunk_texts,
        )

        # 4. LLM call
        job = JobConfig(
            job_id=_JOB_ID,
            model=decision.model_a,
            temperature=0.2,
            max_tokens=decision.budget.max_output_tokens_a,
            response_format={"type": "json_object"},
        )
        result: EvidenceReport | None = await self._llm.call(
            job=job,
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=EvidenceReport,
            run_id=decision.run_id,
        )

        duration_ms = round((time.perf_counter() - t0) * 1000, 2)

        if result is None:
            logger.error(
                "evidence_assembly_failed",
                candidate_id=candidate_id,
                run_id=decision.run_id,
                duration_ms=duration_ms,
            )
            result = self._build_fallback(candidate, decision)

        # 5. Telemetry
        self._record_telemetry(
            metrics=metrics,
            run_id=decision.run_id,
            candidate_id=candidate_id,
            duration_ms=duration_ms,
            report=result,
            prompt_tokens=len(system_prompt) + len(user_message),
            completion_tokens=len(json.dumps(result.model_dump(mode="json"))),
        )

        logger.info(
            "evidence_assembly_complete",
            candidate_id=candidate_id,
            run_id=decision.run_id,
            items_count=len(result.items),
            sufficiency_score=result.sufficiency_score,
            sufficient=result.sufficient,
            duration_ms=duration_ms,
        )
        return result

    # ------------------------------------------------------------------
    # Batch entry point
    # ------------------------------------------------------------------

    async def run_batch(
        self,
        candidates: list[Candidate],
        decisions: list[OrchestratorDecision],
    ) -> dict[str, EvidenceReport]:
        """Assemble and classify evidence for multiple candidates in one LLM call.

        Returns a dict mapping candidate_id -> EvidenceReport.  Candidates
        whose results fail validation get a fallback (sufficient=False).
        """
        from api.agents.models import BatchEvidenceResponse

        assert len(candidates) == len(decisions)
        t0 = time.perf_counter()
        metrics = get_metrics()
        run_id = decisions[0].run_id

        logger.info(
            "evidence_assembly_batch_start",
            run_id=run_id,
            batch_size=len(candidates),
        )

        # Build per-candidate context blocks.
        candidate_blocks: list[str] = []
        for i, (cand, dec) in enumerate(zip(candidates, decisions), 1):
            graph_ctx = await self._gather_graph_context(
                run_id=dec.run_id,
                involved_refs=list(cand.involved_element_refs),
            )
            chunks = await self._retrieve_chunks(
                run_id=dec.run_id,
                involved_refs=list(cand.involved_element_refs),
            )
            cand_json = json.dumps(
                cand.model_dump(mode="json"), indent=2, ensure_ascii=True,
            )
            ctx_json = json.dumps(graph_ctx, indent=2, ensure_ascii=True)
            chunk_text = self._format_chunk_texts(chunks)
            candidate_blocks.append(
                f"### Candidate {i}\n"
                f"#### Candidate Data\n{cand_json}\n\n"
                f"#### Graph Context\n{ctx_json}\n\n"
                f"#### Source Chunks\n{chunk_text}\n"
            )

        candidates_block = "\n---\n\n".join(candidate_blocks)

        # Load batch template and build prompt.
        template = load_prompt_template(_BATCH_JOB_ID, _BATCH_TEMPLATE_VERSION)
        system_prompt: str = template["system_prompt"]
        user_message: str = template["user_template"].format(
            batch_size=len(candidates),
            candidates_block=candidates_block,
        )

        # Compute batch output budget: sum of per-candidate budgets.
        batch_max_output = sum(d.budget.max_output_tokens_a for d in decisions)

        job = JobConfig(
            job_id=_BATCH_JOB_ID,
            model=decisions[0].model_a,
            temperature=0.2,
            max_tokens=batch_max_output,
            response_format={"type": "json_object"},
        )
        batch_result: BatchEvidenceResponse | None = await self._llm.call(
            job=job,
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=BatchEvidenceResponse,
            run_id=run_id,
        )

        duration_ms = round((time.perf_counter() - t0) * 1000, 2)

        # Build results dict, matching by candidate_id.
        results: dict[str, EvidenceReport] = {}

        if batch_result is not None:
            # Index LLM results by candidate_id for lookup.
            llm_results: dict[str, EvidenceReport] = {
                r.candidate_id: r for r in batch_result.results
            }
            for cand, dec in zip(candidates, decisions):
                cid = cand.candidate_id
                report = llm_results.get(cid)
                if report is not None:
                    results[cid] = report
                else:
                    logger.warning(
                        "evidence_batch_missing_candidate",
                        candidate_id=cid,
                        run_id=run_id,
                    )
                    results[cid] = self._build_fallback(cand, dec)
        else:
            logger.error(
                "evidence_assembly_batch_failed",
                run_id=run_id,
                batch_size=len(candidates),
                duration_ms=duration_ms,
            )
            for cand, dec in zip(candidates, decisions):
                results[cand.candidate_id] = self._build_fallback(cand, dec)

        # Telemetry per candidate.
        prompt_tokens = len(system_prompt) + len(user_message)
        for cand in candidates:
            cid = cand.candidate_id
            report = results[cid]
            self._record_telemetry(
                metrics=metrics,
                run_id=run_id,
                candidate_id=cid,
                duration_ms=duration_ms / len(candidates),
                report=report,
                prompt_tokens=prompt_tokens // len(candidates),
                completion_tokens=len(json.dumps(report.model_dump(mode="json"))),
            )

        logger.info(
            "evidence_assembly_batch_complete",
            run_id=run_id,
            batch_size=len(candidates),
            duration_ms=duration_ms,
            sufficient_count=sum(1 for r in results.values() if r.sufficient),
        )
        return results

    # ------------------------------------------------------------------
    # Graph context gathering
    # ------------------------------------------------------------------

    async def _gather_graph_context(
        self, run_id: str, involved_refs: list[str],
    ) -> list[dict[str, Any]]:
        """Retrieve neighbour nodes for each involved element reference.

        Cached internally by GraphReader via CacheKey.graph_query (5 min).
        """
        reader = self._get_graph_reader()
        context: list[dict[str, Any]] = []
        for ref in involved_refs:
            try:
                neighbors: NeighborResult = await reader.get_neighbors(
                    run_id=run_id, dedupe_key=ref,
                )
                context.append({
                    "element_ref": ref,
                    "neighbors": [
                        {
                            "dedupe_key": n.dedupe_key,
                            "node_type": n.node_type,
                            "properties": n.properties,
                        }
                        for n in neighbors.neighbors
                    ],
                })
            except Exception as exc:
                logger.warning(
                    "evidence_graph_context_error",
                    run_id=run_id, element_ref=ref, error=str(exc),
                )
                context.append({"element_ref": ref, "neighbors": []})
        return context

    # ------------------------------------------------------------------
    # Chunk retrieval with caching
    # ------------------------------------------------------------------

    async def _retrieve_chunks(
        self, run_id: str, involved_refs: list[str],
    ) -> list[EvidenceChunk]:
        """Retrieve evidence chunks per involved ref; dedup and cache."""
        retriever = self._get_retriever()
        cache = self._get_cache()
        seen_ids: set[str] = set()
        all_chunks: list[EvidenceChunk] = []

        for ref in involved_refs:
            try:
                chunks = await retriever.by_dedupe_key(
                    run_id=run_id, dedupe_key=ref, top_k=_MAX_CHUNKS_PER_REF,
                )
            except Exception as exc:
                logger.warning(
                    "evidence_chunk_retrieval_error",
                    run_id=run_id, element_ref=ref, error=str(exc),
                )
                continue

            for chunk in chunks:
                if chunk.chunk_id in seen_ids:
                    continue
                seen_ids.add(chunk.chunk_id)
                all_chunks.append(chunk)
                # Backfill chunk text cache (write-through).
                await cache.set(
                    CacheKey.chunk(chunk_id=chunk.chunk_id),
                    CachedChunkText(chunk_id=chunk.chunk_id, text=chunk.text),
                    ttl=CHUNK_CACHE_TTL,
                )

        logger.debug(
            "evidence_chunks_gathered",
            run_id=run_id,
            total_chunks=len(all_chunks),
            involved_refs_count=len(involved_refs),
        )
        return all_chunks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_chunk_texts(chunks: list[EvidenceChunk]) -> str:
        """Format chunks into a delimited string for the LLM prompt.

        Chunk text is included but never logged (SKILL-D R-D5).
        """
        if not chunks:
            return "(No source chunks available)"
        parts: list[str] = []
        for chunk in chunks:
            header = (
                f"[{chunk.chunk_id}] "
                f"(doc: {chunk.doc_id}, loc: {chunk.start_page_locator})"
            )
            parts.append(f"{header}\n{chunk.text}")
        return _CHUNK_DELIMITER.join(parts)

    @staticmethod
    def _build_fallback(
        candidate: Candidate, decision: OrchestratorDecision,
    ) -> EvidenceReport:
        """Fail-closed fallback: no items, sufficient=False."""
        return EvidenceReport(
            candidate_id=candidate.candidate_id,
            items=(),
            sufficiency_score=0.0,
            sufficient=False,
            run_id=decision.run_id,
            schema_version=decision.schema_version,
        )

    @staticmethod
    def _record_telemetry(
        metrics: Any,
        run_id: str,
        candidate_id: str,
        duration_ms: float,
        report: EvidenceReport,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Record per-execution telemetry keyed by (run_id, candidate_id, agent)."""
        labels = {"run_id": run_id, "candidate_id": candidate_id, "agent": _AGENT_NAME}
        metrics.increment("agent_calls", **labels)
        metrics.observe("agent_duration_ms", duration_ms, **labels)
        metrics.observe("agent_prompt_tokens", float(prompt_tokens), **labels)
        metrics.observe("agent_completion_tokens", float(completion_tokens), **labels)
        metrics.observe("agent_evidence_score", report.sufficiency_score, **labels)
        total_tokens = prompt_tokens + completion_tokens
        metrics.observe(
            "agent_cost_estimate", (total_tokens / 1000.0) * _COST_PER_1K_TOKENS, **labels,
        )
