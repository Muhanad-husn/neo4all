"""
api/agents/proposal.py -- Agent-P: Proposal Composer (SPEC-07 S-07.4).

LLM agent that receives a candidate + EvidenceReport (from Agent-A, possibly
augmented by Agent-B), sends to LLM with proposal_composer prompt, and
produces a governed ProposalPacket.

Security guards (CLAUDE.md S4.5 -- sandboxed intelligence):
  - Pydantic validation of AgentProposalOutput (fail-closed on malformed).
  - Regex guard rejects any output containing Cypher query syntax or executable
    instructions.  Defence-in-depth: the prompt forbids it, Pydantic validates
    structure, and this module scans free-text fields for forbidden patterns.

Pipeline integration:
  - Converts AgentProposalOutput -> ProposalPacket (api/proposals/models.py).
  - Submits via ProposalService.create() -- same governed pipeline as manual
    curation (propose -> diff -> approval queue).  No bypass.

Telemetry:
  - Records per-execution metrics keyed by (run_id, candidate_id, agent-p).

Sensitive data (SKILL-D R-D5):
  - Chunk text is never logged.
  - Only structural metadata (candidate_id, proposal_class, confidence_score)
    appears in log entries.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from api.agents.evidence import load_prompt_template
from api.agents.models import AgentProposalOutput, EvidenceReport
from api.agents.orchestrator import OrchestratorDecision
from api.agents.structural import compute_structural_recommendation
from api.cache.client import CacheClient, get_cache_client
from api.cache.keys import CacheKey
from api.models.candidate import Candidate, CandidateLane, CandidateType
from api.observability.logger import get_logger
from api.observability.metrics import get_metrics
from api.proposals.models import ElementRef, ProposalClass, ProposalPacket
from api.proposals.service import ProposalService, ProposalValidationError
from api.proposals.storage import ProposalStorageError
from api.schema.models import SchemaVersion
from api.services.llm import JobConfig, LLMClient

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JOB_ID: str = "proposal_composer"
_TEMPLATE_VERSION: str = "v11"
_BATCH_TEMPLATE_VERSION: str = "v14"
_AGENT_NAME: str = "agent-p"
_COST_PER_1K_TOKENS: float = 0.001


# ---------------------------------------------------------------------------
# Safety guard patterns (CLAUDE.md S4.5)
# ---------------------------------------------------------------------------

# Cypher query syntax -- parenthesised node patterns and property access.
# Does NOT match bare English words ("merge", "match", "create") -- only
# patterns with Cypher-specific punctuation.
_CYPHER_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:"
    r"MATCH\s*\(|"              # MATCH (n:Label)
    r"CREATE\s*\(|"             # CREATE (n:Label {})
    r"MERGE\s*\(|"              # MERGE (n:Label)
    r"DETACH\s+DELETE\b|"       # DETACH DELETE n
    r"SET\s+\w+\.\w+|"         # SET n.prop = ...
    r"REMOVE\s+\w+\.\w+|"      # REMOVE n.prop
    r"FOREACH\s*\("             # FOREACH (x IN ...)
    r")",
    re.IGNORECASE,
)

# Executable code patterns -- code blocks, Python execution primitives.
_EXECUTABLE_PATTERN: re.Pattern[str] = re.compile(
    r"(?:"
    r"```|"                     # Markdown code block markers
    r"\bexec\s*\(|"            # exec()
    r"\beval\s*\(|"            # eval()
    r"\b__import__\s*\(|"      # __import__()
    r"\bsubprocess\.\w+|"      # subprocess.run / subprocess.call
    r"\bos\.system\s*\("       # os.system()
    r")",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Safety exception
# ---------------------------------------------------------------------------


class ProposalSafetyError(Exception):
    """Raised when Agent-P output contains forbidden Cypher or executable content.

    Attributes:
        field:        Name of the field that triggered the guard.
        pattern_name: Category of the forbidden pattern.
        match:        Matched substring.
    """

    def __init__(self, field: str, pattern_name: str, match: str) -> None:
        super().__init__(
            f"Agent-P output contains forbidden {pattern_name} in field "
            f"'{field}': matched '{match}'"
        )
        self.field = field
        self.pattern_name = pattern_name
        self.match = match


# ---------------------------------------------------------------------------
# Guard function
# ---------------------------------------------------------------------------


def _guard_no_cypher_or_executable(output: AgentProposalOutput) -> None:
    """Reject AgentProposalOutput containing Cypher syntax or executable code.

    Scans the rationale and rule_ids fields (the only free-text fields in the
    output contract) for forbidden patterns.  Raises ProposalSafetyError on
    first match.

    Other fields are structurally constrained:
      - candidate_id: 64-char hex (validated by Pydantic).
      - proposal_class: enum member (validated by Pydantic).
      - evidence_ids: hex digest strings (no free text).
      - confidence_score: float (no free text).
    """
    texts: dict[str, str] = {"rationale": output.rationale}
    for i, rule_id in enumerate(output.rule_ids):
        texts[f"rule_ids[{i}]"] = rule_id

    for field_name, text in texts.items():
        cypher_hit = _CYPHER_PATTERN.search(text)
        if cypher_hit:
            raise ProposalSafetyError(
                field=field_name,
                pattern_name="Cypher query syntax",
                match=cypher_hit.group(0),
            )
        exec_hit = _EXECUTABLE_PATTERN.search(text)
        if exec_hit:
            raise ProposalSafetyError(
                field=field_name,
                pattern_name="executable instruction",
                match=exec_hit.group(0),
            )


def _validate_normalize_target(
    output: AgentProposalOutput,
    candidate: Candidate,
) -> AgentProposalOutput:
    """Ensure normalize proposals for canonical violations have actionable targets.

    Direction violations (node types don't match any schema edge) require a
    ``normalize_target`` — without it, the diff builder falls through to a
    property-update with empty properties, which is a no-op.  The violation
    persists, is re-detected, and the pipeline loops forever.

    Inverse violations (correct node types, wrong direction) can be auto-filled:
    the fix is to re-create the edge with the same type but swapped endpoints.

    Returns the original output unchanged when no correction is needed.
    """
    if output.proposal_class != "normalize":
        return output
    if candidate.candidate_type != CandidateType.canonical_violation:
        return output
    if candidate.candidate_lane != CandidateLane.relationship:
        return output
    if output.normalize_target:
        return output  # target provided, nothing to fix

    ctx = candidate.collision_context

    # Inverse violation: same type, just need to swap endpoints.
    # Auto-fill normalize_target with the current rel_type.
    if candidate.detection_method == "canonical_inverse_violation":
        rel_type = ctx.get("rel_type", "")
        if rel_type:
            logger.info(
                "proposal_normalize_auto_target",
                candidate_id=output.candidate_id,
                detection_method=candidate.detection_method,
                auto_target=rel_type,
            )
            return AgentProposalOutput(
                candidate_id=output.candidate_id,
                proposal_class="normalize",
                rule_ids=output.rule_ids,
                evidence_ids=output.evidence_ids,
                rationale=output.rationale,
                confidence_score=output.confidence_score,
                high_risk_override=output.high_risk_override,
                normalize_target=rel_type,
            )

    # Direction violation without target: normalize is a no-op → defer.
    # The LLM said "normalize" but couldn't find an existing schema type
    # that fits these node types.  A human must decide: rename (extends
    # schema, high-risk) or delete.
    logger.warning(
        "proposal_normalize_no_target_deferred",
        candidate_id=output.candidate_id,
        detection_method=candidate.detection_method,
    )
    return AgentProposalOutput(
        candidate_id=output.candidate_id,
        proposal_class="defer",
        rule_ids=output.rule_ids,
        evidence_ids=output.evidence_ids,
        rationale=(
            f"{output.rationale} "
            "[Deferred: no existing schema type fits this node pair. "
            "Requires manual rename (schema extension) or delete.]"
        ),
        confidence_score=min(output.confidence_score, 0.30),
        high_risk_override=False,
        normalize_target="",
    )


# ---------------------------------------------------------------------------
# ProposalComposerAgent (Agent-P)
# ---------------------------------------------------------------------------


class ProposalComposerAgent:
    """LLM agent that composes Proposal Packets for curation candidates.

    Receives candidate + EvidenceReport, sends to LLM with proposal_composer
    prompt, validates the response, applies safety guards, and converts to a
    governed ProposalPacket submitted through the standard approval pipeline.

    All dependencies are injectable for testability.
    """

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        cache: CacheClient | None = None,
        proposal_service: ProposalService | None = None,
    ) -> None:
        self._llm = llm_client or LLMClient()
        self._cache = cache
        self._proposal_service = proposal_service

    def _get_cache(self) -> CacheClient:
        if self._cache is None:
            self._cache = get_cache_client()
        return self._cache

    def _get_proposal_service(self) -> ProposalService:
        if self._proposal_service is None:
            self._proposal_service = ProposalService()
        return self._proposal_service

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        candidate: Candidate,
        decision: OrchestratorDecision,
        evidence_report: EvidenceReport,
    ) -> ProposalPacket | None:
        """Compose a ProposalPacket and submit to the governed pipeline.

        Steps:
        1. Gather schema rules for LLM context.
        2. Build LLM prompt from proposal_composer/v1.yaml template.
        3. Call LLM; validate response as AgentProposalOutput.
        4. Apply safety guard (no Cypher, no executable instructions).
        5. Convert AgentProposalOutput -> ProposalPacket.
        6. Submit via ProposalService.create() (same pipeline as manual).
        7. Record telemetry.

        Returns:
            ProposalPacket on success, None on any failure (fail-closed).
        """
        t0 = time.perf_counter()
        metrics = get_metrics()
        candidate_id = candidate.candidate_id

        logger.info(
            "proposal_composition_start",
            candidate_id=candidate_id,
            run_id=decision.run_id,
            evidence_items=len(evidence_report.items),
            evidence_sufficient=evidence_report.sufficient,
        )

        # 1. Compute and attach structural recommendation (advisory context for LLM).
        if evidence_report.structural_recommendation is None:
            rec = compute_structural_recommendation(candidate)
            evidence_report = evidence_report.model_copy(
                update={"structural_recommendation": rec},
            )

        # 2. Schema rules for contextualising the LLM prompt.
        schema_rules = await self._gather_schema_rules(run_id=decision.run_id)

        # 2b. Pre-canonicalize collision_context values so the LLM cannot
        #     hide behind case/whitespace differences as an excuse to propose
        #     canonicalize instead of merge.  Title-case value_a / value_b.
        candidate_for_llm = candidate.model_dump(mode="json")
        ctx_copy = dict(candidate_for_llm.get("collision_context", {}))
        for vk in ("value_a", "value_b"):
            if vk in ctx_copy and isinstance(ctx_copy[vk], str):
                ctx_copy[vk] = ctx_copy[vk].strip().title()
        ctx_copy["_canonicalization_applied"] = True
        candidate_for_llm["collision_context"] = ctx_copy

        # 3. Build prompt from versioned template.
        template = load_prompt_template(_JOB_ID, _TEMPLATE_VERSION)
        system_prompt: str = template["system_prompt"]

        candidate_json = json.dumps(
            candidate_for_llm, indent=2, ensure_ascii=True,
        )
        evidence_json = json.dumps(
            evidence_report.model_dump(mode="json"), indent=2, ensure_ascii=True,
        )
        schema_rules_json = json.dumps(
            schema_rules, indent=2, ensure_ascii=True,
        )
        user_message: str = template["user_template"].format(
            candidate_json=candidate_json,
            evidence_json=evidence_json,
            schema_rules=schema_rules_json,
        )

        # 3. LLM call — fail-closed (returns None on any failure).
        job = JobConfig(
            job_id=_JOB_ID,
            model=decision.model_p,
            temperature=0.2,
            max_tokens=decision.budget.max_output_tokens_p,
            response_format={"type": "json_object"},
        )
        raw_output: AgentProposalOutput | None = await self._llm.call(
            job=job,
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=AgentProposalOutput,
            run_id=decision.run_id,
        )

        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        prompt_tokens = len(system_prompt) + len(user_message)

        if raw_output is None:
            logger.error(
                "proposal_composition_llm_failed",
                candidate_id=candidate_id,
                run_id=decision.run_id,
                duration_ms=duration_ms,
            )
            self._record_telemetry(
                metrics, decision.run_id, candidate_id, duration_ms,
                prompt_tokens, 0, success=False,
            )
            return None

        completion_tokens = len(json.dumps(raw_output.model_dump(mode="json")))

        # 4. Safety guard — reject Cypher or executable instructions.
        try:
            _guard_no_cypher_or_executable(raw_output)
        except ProposalSafetyError as exc:
            logger.error(
                "proposal_composition_safety_violation",
                candidate_id=candidate_id,
                run_id=decision.run_id,
                field=exc.field,
                pattern=exc.pattern_name,
                match=exc.match,
                duration_ms=duration_ms,
            )
            self._record_telemetry(
                metrics, decision.run_id, candidate_id, duration_ms,
                prompt_tokens, completion_tokens, success=False,
            )
            return None

        # 4b. Validate normalize target for canonical violations.
        #     Prevents no-op normalize proposals that cause infinite
        #     re-detection loops (direction violations without a target type).
        raw_output = _validate_normalize_target(raw_output, candidate)

        # 5. Convert AgentProposalOutput -> ProposalPacket.
        packet = self._build_proposal_packet(
            output=raw_output,
            candidate=candidate,
            decision=decision,
            evidence_report=evidence_report,
        )

        # 6. Submit via ProposalService — same governed pipeline as manual.
        try:
            stored = await self._get_proposal_service().create(packet)
        except ProposalValidationError as exc:
            logger.error(
                "proposal_composition_validation_failed",
                candidate_id=candidate_id,
                run_id=decision.run_id,
                proposal_id=packet.proposal_id,
                error=str(exc),
                duration_ms=duration_ms,
            )
            self._record_telemetry(
                metrics, decision.run_id, candidate_id, duration_ms,
                prompt_tokens, completion_tokens, success=False,
            )
            return None
        except ProposalStorageError as exc:
            logger.error(
                "proposal_composition_storage_failed",
                candidate_id=candidate_id,
                run_id=decision.run_id,
                proposal_id=packet.proposal_id,
                error=str(exc),
                duration_ms=duration_ms,
            )
            self._record_telemetry(
                metrics, decision.run_id, candidate_id, duration_ms,
                prompt_tokens, completion_tokens, success=False,
            )
            return None

        # 7. Telemetry — full pipeline duration including storage.
        final_duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        self._record_telemetry(
            metrics, decision.run_id, candidate_id, final_duration_ms,
            prompt_tokens, completion_tokens, success=True,
        )

        logger.info(
            "proposal_composition_complete",
            candidate_id=candidate_id,
            run_id=decision.run_id,
            proposal_id=stored.proposal_id,
            proposal_class=str(stored.proposal_class),
            confidence_score=raw_output.confidence_score,
            duration_ms=final_duration_ms,
        )
        return stored

    # ------------------------------------------------------------------
    # Batch entry point
    # ------------------------------------------------------------------

    async def run_batch(
        self,
        candidates: list[Candidate],
        decisions: list[OrchestratorDecision],
        evidence_reports: list[EvidenceReport],
    ) -> dict[str, ProposalPacket | None]:
        """Compose proposals for multiple candidates in one LLM call.

        Deterministic fast-paths (chain group merges) are handled inline
        without the LLM.  Remaining candidates go to a single batch LLM call.

        Returns a dict mapping candidate_id -> ProposalPacket (or None on
        failure).  Per-candidate errors are isolated — one bad result does
        not block the others.
        """
        from api.agents.models import BatchProposalResponse

        assert len(candidates) == len(decisions) == len(evidence_reports)
        t0 = time.perf_counter()
        metrics = get_metrics()
        run_id = decisions[0].run_id

        logger.info(
            "proposal_composition_batch_start",
            run_id=run_id,
            batch_size=len(candidates),
        )

        results: dict[str, ProposalPacket | None] = {}

        # Attach structural recommendation (advisory context for LLM).
        llm_candidates: list[tuple[Candidate, OrchestratorDecision, EvidenceReport]] = []
        for cand, dec, ev in zip(candidates, decisions, evidence_reports):
            if ev.structural_recommendation is None:
                rec = compute_structural_recommendation(cand)
                ev = ev.model_copy(update={"structural_recommendation": rec})
            llm_candidates.append((cand, dec, ev))

        if not llm_candidates:
            return results

        # Gather schema rules once (shared context).
        schema_rules = await self._gather_schema_rules(run_id=run_id)
        schema_rules_json = json.dumps(schema_rules, indent=2, ensure_ascii=True)

        # Build per-candidate blocks.
        candidate_blocks: list[str] = []
        for i, (cand, dec, ev) in enumerate(llm_candidates, 1):
            # Pre-canonicalize collision_context (same as single-candidate path).
            cand_dict = cand.model_dump(mode="json")
            ctx_copy = dict(cand_dict.get("collision_context", {}))
            for vk in ("value_a", "value_b"):
                if vk in ctx_copy and isinstance(ctx_copy[vk], str):
                    ctx_copy[vk] = ctx_copy[vk].strip().title()
            ctx_copy["_canonicalization_applied"] = True
            cand_dict["collision_context"] = ctx_copy

            cand_json = json.dumps(cand_dict, indent=2, ensure_ascii=True)
            ev_json = json.dumps(
                ev.model_dump(mode="json"), indent=2, ensure_ascii=True,
            )
            candidate_blocks.append(
                f"### Candidate {i}\n"
                f"#### Candidate Data\n{cand_json}\n\n"
                f"#### Evidence Report\n{ev_json}\n"
            )

        candidates_block = "\n---\n\n".join(candidate_blocks)

        # Load batch template and build prompt.
        template = load_prompt_template(_JOB_ID, _BATCH_TEMPLATE_VERSION)
        system_prompt: str = template["system_prompt"]
        user_message: str = template["user_template"].format(
            batch_size=len(llm_candidates),
            schema_rules=schema_rules_json,
            candidates_block=candidates_block,
        )

        # Compute batch output budget.
        batch_max_output = sum(d.budget.max_output_tokens_p for _, d, _ in llm_candidates)

        job = JobConfig(
            job_id=_JOB_ID,
            model=llm_candidates[0][1].model_p,
            temperature=0.2,
            max_tokens=batch_max_output,
            response_format={"type": "json_object"},
        )
        batch_result: BatchProposalResponse | None = await self._llm.call(
            job=job,
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=BatchProposalResponse,
            run_id=run_id,
        )

        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        prompt_tokens = len(system_prompt) + len(user_message)

        if batch_result is None:
            logger.error(
                "proposal_composition_batch_llm_failed",
                run_id=run_id,
                batch_size=len(llm_candidates),
                duration_ms=duration_ms,
            )
            for cand, dec, _ in llm_candidates:
                results[cand.candidate_id] = None
                self._record_telemetry(
                    metrics, run_id, cand.candidate_id, duration_ms,
                    prompt_tokens, 0, success=False,
                )
            return results

        # Index LLM results by candidate_id.
        llm_outputs: dict[str, AgentProposalOutput] = {
            r.candidate_id: r for r in batch_result.results
        }

        # Process each LLM candidate result with safety guards.
        for cand, dec, ev in llm_candidates:
            cid = cand.candidate_id
            raw_output = llm_outputs.get(cid)

            if raw_output is None:
                logger.warning(
                    "proposal_batch_missing_candidate",
                    candidate_id=cid,
                    run_id=run_id,
                )
                results[cid] = None
                self._record_telemetry(
                    metrics, run_id, cid, duration_ms / len(llm_candidates),
                    prompt_tokens // len(llm_candidates), 0, success=False,
                )
                continue

            completion_tokens = len(json.dumps(raw_output.model_dump(mode="json")))

            # Safety guard — reject Cypher or executable instructions.
            try:
                _guard_no_cypher_or_executable(raw_output)
            except ProposalSafetyError as exc:
                logger.error(
                    "proposal_composition_safety_violation",
                    candidate_id=cid,
                    run_id=run_id,
                    field=exc.field,
                    pattern=exc.pattern_name,
                    match=exc.match,
                )
                results[cid] = None
                self._record_telemetry(
                    metrics, run_id, cid, duration_ms / len(llm_candidates),
                    prompt_tokens // len(llm_candidates), completion_tokens,
                    success=False,
                )
                continue

            # Validate normalize target for canonical violations.
            raw_output = _validate_normalize_target(raw_output, cand)

            # Build ProposalPacket.
            packet = self._build_proposal_packet(
                output=raw_output,
                candidate=cand,
                decision=dec,
                evidence_report=ev,
            )

            # Submit via ProposalService.
            try:
                stored = await self._get_proposal_service().create(packet)
            except (ProposalValidationError, ProposalStorageError) as exc:
                logger.error(
                    "proposal_batch_storage_failed",
                    candidate_id=cid,
                    run_id=run_id,
                    error=str(exc),
                )
                results[cid] = None
                self._record_telemetry(
                    metrics, run_id, cid, duration_ms / len(llm_candidates),
                    prompt_tokens // len(llm_candidates), completion_tokens,
                    success=False,
                )
                continue

            results[cid] = stored
            self._record_telemetry(
                metrics, run_id, cid, duration_ms / len(llm_candidates),
                prompt_tokens // len(llm_candidates), completion_tokens,
                success=True,
            )

            logger.info(
                "proposal_composition_complete",
                candidate_id=cid,
                run_id=run_id,
                proposal_id=stored.proposal_id,
                proposal_class=str(stored.proposal_class),
                confidence_score=raw_output.confidence_score,
                batch_mode=True,
            )

        logger.info(
            "proposal_composition_batch_complete",
            run_id=run_id,
            batch_size=len(llm_candidates),
            llm_count=len(llm_candidates),
            success_count=sum(1 for v in results.values() if v is not None),
            duration_ms=duration_ms,
        )
        return results

    # ------------------------------------------------------------------
    # Schema rules gathering
    # ------------------------------------------------------------------

    async def _gather_schema_rules(
        self, run_id: str,
    ) -> list[dict[str, Any]]:
        """Retrieve schema rules for contextualising the LLM prompt.

        Returns a list of dicts describing node and edge type constraints
        from the locked schema.  Returns an empty list if the schema is
        not found — Agent-P must still produce a valid proposal; it just
        has less context.
        """
        cache = self._get_cache()
        schema: SchemaVersion | None = await cache.get(
            CacheKey.schema(run_id=run_id), model=SchemaVersion,
        )
        if schema is None:
            logger.warning("proposal_schema_not_found", run_id=run_id)
            return []

        rules: list[dict[str, Any]] = []
        for node_type in schema.nodes:
            rules.append({
                "rule_type": "node_type",
                "rule_id": f"node:{node_type.type}",
                "node_class": node_type.node_class,
                "type": node_type.type,
                "primary_property": node_type.primary_property,
                "additional_properties": list(node_type.additional_properties),
            })
        for edge_type in schema.edges:
            rules.append({
                "rule_type": "edge_type",
                "rule_id": f"edge:{edge_type.type}",
                "type": edge_type.type,
                "start_node_type": edge_type.start_node_type,
                "end_node_type": edge_type.end_node_type,
                "additional_properties": list(edge_type.additional_properties),
            })
        return rules

    # ------------------------------------------------------------------
    # ProposalPacket builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_proposal_packet(
        output: AgentProposalOutput,
        candidate: Candidate,
        decision: OrchestratorDecision,
        evidence_report: EvidenceReport,
    ) -> ProposalPacket:
        """Convert AgentProposalOutput into a governed ProposalPacket.

        Assembles all linkage, evidence, governance, and target fields
        required by the Diff Builder and Approval Gate.  The proposal_id
        is computed deterministically from (run_id, candidate_id,
        proposal_class) by the ProposalPacket model.
        """
        # Build target ElementRefs from the candidate's involved elements.
        # For relationship-lane candidates (canonical_violation, exact_rel_duplicate),
        # the collision_context identifies the relationship dedupe_key explicitly.
        # All other refs in involved_element_refs are endpoint nodes.
        #
        # For group merge candidates (duplicate_chain_group), the refs are
        # already ordered: survivor first (set during candidate generation).
        # The diff builder uses targets[0] as the merge survivor.
        rel_dk: str | None = None
        if candidate.candidate_lane == CandidateLane.relationship:
            rel_dk = candidate.collision_context.get("rel_dedupe_key") or (
                candidate.collision_context.get("dedupe_key")
            )

        targets_list: list[ElementRef] = []
        ctx = candidate.collision_context
        for ref in candidate.involved_element_refs:
            if rel_dk and ref == rel_dk:
                element_type = "relationship"
                # For normalize re-type: carry metadata so the diff builder
                # can generate delete_edge + create_edge instead of update_edge.
                props: dict[str, Any] = {}
                normalize_target = getattr(output, "normalize_target", "") or ""
                if (
                    output.proposal_class == "normalize"
                    and normalize_target
                ):
                    props["_target_rel_type"] = normalize_target
                    props["_old_rel_type"] = ctx.get("rel_type", "")
                    # Inverse violations: swap start/end to fix direction.
                    if candidate.detection_method == "canonical_inverse_violation":
                        props["_start_dedupe_key"] = ctx.get("end_dedupe_key", "")
                        props["_end_dedupe_key"] = ctx.get("start_dedupe_key", "")
                    else:
                        props["_start_dedupe_key"] = ctx.get("start_dedupe_key", "")
                        props["_end_dedupe_key"] = ctx.get("end_dedupe_key", "")
                targets_list.append(
                    ElementRef(
                        element_type=element_type,
                        dedupe_key=ref,
                        label=ref,
                        properties=props,
                    )
                )
            elif candidate.candidate_lane == CandidateLane.relationship:
                element_type = "node"  # endpoint node ref
                targets_list.append(
                    ElementRef(
                        element_type=element_type,
                        dedupe_key=ref,
                        label=ref,
                    )
                )
            else:
                element_type = "node"
                targets_list.append(
                    ElementRef(
                        element_type=element_type,
                        dedupe_key=ref,
                        label=ref,
                    )
                )
        targets = tuple(targets_list)

        # Collect unique doc_ids from the evidence report for provenance.
        evidence_doc_ids = tuple(sorted({
            item.source_doc for item in evidence_report.items
        }))

        return ProposalPacket(
            run_id=decision.run_id,
            candidate_id=output.candidate_id,
            proposal_class=ProposalClass(output.proposal_class),
            schema_version=decision.schema_version,
            evidence_chunk_ids=output.evidence_ids,
            evidence_doc_ids=evidence_doc_ids,
            targets=targets,
            rule_ids=output.rule_ids,
            rationale=output.rationale,
            confidence_score=output.confidence_score,
            high_risk_override=output.high_risk_override,
        )

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    @staticmethod
    def _record_telemetry(
        metrics: Any,
        run_id: str,
        candidate_id: str,
        duration_ms: float,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        success: bool,
    ) -> None:
        """Record per-execution telemetry keyed by (run_id, candidate_id, agent)."""
        labels = {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "agent": _AGENT_NAME,
        }
        metrics.increment("agent_calls", **labels)
        metrics.observe("agent_duration_ms", duration_ms, **labels)
        metrics.observe("agent_prompt_tokens", float(prompt_tokens), **labels)
        metrics.observe("agent_completion_tokens", float(completion_tokens), **labels)
        total_tokens = prompt_tokens + completion_tokens
        metrics.observe(
            "agent_cost_estimate",
            (total_tokens / 1000.0) * _COST_PER_1K_TOKENS,
            **labels,
        )
        if success:
            metrics.increment("agent_proposals_created", **labels)
        else:
            metrics.increment("agent_proposals_failed", **labels)
