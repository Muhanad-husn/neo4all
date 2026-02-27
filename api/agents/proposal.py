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
from api.cache.client import CacheClient, get_cache_client
from api.cache.keys import CacheKey
from api.models.candidate import Candidate
from api.observability.logger import get_logger
from api.observability.metrics import get_metrics
from api.proposals.models import ElementRef, ProposalClass, ProposalPacket
from api.proposals.service import ProposalService
from api.schema.models import SchemaVersion
from api.services.llm import JobConfig, LLMClient

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JOB_ID: str = "proposal_composer"
_TEMPLATE_VERSION: str = "v1"
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

        # 1. Schema rules for contextualising the LLM prompt.
        schema_rules = await self._gather_schema_rules(run_id=decision.run_id)

        # 2. Build prompt from versioned template.
        template = load_prompt_template(_JOB_ID, _TEMPLATE_VERSION)
        system_prompt: str = template["system_prompt"]

        candidate_json = json.dumps(
            candidate.model_dump(mode="json"), indent=2, ensure_ascii=True,
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
        except Exception as exc:
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
        targets = tuple(
            ElementRef(
                element_type="node",
                dedupe_key=ref,
                label=ref,
            )
            for ref in candidate.involved_element_refs
        )

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
