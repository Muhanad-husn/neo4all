# SPEC-07: AI-Powered Curation Agent Pipeline (Layer 3)

**Increment**: 7 | **Version target**: 0.7.0 | **Prerequisites**: SPEC-06 complete
**Skills required**: SKILL-A, SKILL-B, SKILL-D

---

## Objective

Implement the multi-agent curation pipeline: Orchestrator → Agent-A → Agent-B → Agent-P → Diff Builder → Human Approval → Agent-C. All AI proposals flow through the same governed pipeline as manual curation. Add agent telemetry.

---

## Specifications

### S-07.1: Orchestrator
Create `api/agents/orchestrator.py`: non-LLM. Assigns risk class (low/medium/high) and tool budget (token limit, cost limit, max retrieval rounds) per candidate. Loop guards prevent runaway processing.

### S-07.2: Agent-A (Evidence Assembly)
Create `api/agents/evidence.py`: LLM agent. Receives candidate + graph context, retrieves chunks from Qdrant via `api/vector/retriever.py`, classifies evidence (Supporting, Corroborating, Conflicting), computes sufficiency score. Output: typed `EvidenceReport`. Does not decide actions.

### S-07.3: Agent-B (Retrieval Augmentation)
Create `api/agents/retrieval.py`: LLM agent. Triggered only if Agent-A flags insufficient evidence. Targeted retrieval within budget. Loop-guarded (max N rounds). Still insufficient → defer.

### S-07.4: Agent-P (Proposal Composer)
Create `api/agents/proposal.py`: LLM agent. Receives candidate + evidence report, selects proposal class, cites rule IDs and evidence IDs, writes rationale. Outputs Proposal Packet. Never builds diffs or executes.

### S-07.5: Agent Output Contracts
Create `api/agents/models.py`: `EvidenceReport`, `RetrievalResult`, `AgentProposalOutput`. Validated on every response. Malformed → fail-closed.

### S-07.6: Prompt Templates
Create `prompts/evidence_assembly/v1.yaml`, `prompts/retrieval_augmentation/v1.yaml`, `prompts/proposal_composer/v1.yaml`.

### S-07.7: Pipeline Jobs
Update `api/worker/jobs.py`: `evidence_assembly_job`, `retrieval_augmentation_job`, `proposal_composition_job`. Chained execution.

### S-07.8: Model-Per-Job Config
Extend `api/config.py`: per-agent model assignment, token/cost budgets, reranking top-N.

### S-07.9: UI
Update `ui/pages/curation.py`: batch candidate processing, agent chain progress, evidence reports, AI proposals with rationale, pending approval queue.

### S-07.10: Agent Telemetry
Update `api/observability/metrics.py`: per-agent metrics (token usage in/out, cost estimate, execution time, evidence score) keyed by `(run_id, candidate_id, agent_name)`.
Update `api/routers/monitoring.py`: `GET /api/monitoring/metrics` (aggregated LLM usage), `GET /api/monitoring/agents/{run_id}` (per-candidate telemetry).
Update `ui/pages/monitoring.py`: agent pipeline panel (per-agent tokens, cost, timing), budget consumption gauges.

### S-07.11: Agent Pipeline Caching
Cache graph context via `CacheKey.graph_query(run_id, query_hash)` and chunk text via `CacheKey.chunk(chunk_id)`. Cache invalidated after Agent-C execution.

---

## Files to Generate

| # | File Path | Purpose |
|---|-----------|---------|
| 1 | `api/agents/models.py` | Agent output contracts |
| 2 | `api/agents/orchestrator.py` | Orchestrator |
| 3 | `api/agents/evidence.py` | Agent-A |
| 4 | `api/agents/retrieval.py` | Agent-B |
| 5 | `api/agents/proposal.py` | Agent-P |
| 6 | `prompts/evidence_assembly/v1.yaml` | Agent-A prompt |
| 7 | `prompts/retrieval_augmentation/v1.yaml` | Agent-B prompt |
| 8 | `prompts/proposal_composer/v1.yaml` | Agent-P prompt |
| 9 | `api/worker/jobs.py` | Pipeline jobs (update) |
| 10 | `api/config.py` | Per-agent config (update) |
| 11 | `api/observability/metrics.py` | Agent telemetry (update) |
| 12 | `api/routers/monitoring.py` | Agent monitoring (update) |
| 13 | `ui/pages/curation.py` | Layer 3 UI (update) |
| 14 | `ui/pages/monitoring.py` | Telemetry panel (update) |
| 15 | `tests/unit/test_orchestrator_*.py` | Risk, budget, loop guard |
| 16 | `tests/unit/test_agent_output_contracts.py` | Contract validation |
| 17 | `tests/unit/test_agent_fallback.py` | Fallback on failure |
| 18 | `fixtures/agent_pipeline/*.json` | Sample candidates, evidence |

---

## Acceptance Criteria

- [ ] Orchestrator assigns risk class and budgets correctly
- [ ] Agent-A produces typed evidence report; malformed → fallback
- [ ] Agent-B only triggers on insufficient evidence; respects loop guard
- [ ] Agent-P produces valid Proposal Packet — no Cypher or executable instructions
- [ ] Full pipeline: candidate → agents → proposal → diff → approval queue
- [ ] Budget enforcement prevents token overrun
- [ ] Agent telemetry recorded per execution
- [ ] `/api/monitoring/agents/{run_id}` returns per-candidate telemetry
- [ ] Monitoring UI shows per-agent tokens and cost
- [ ] Graph context and chunk text cached
- [ ] Cache invalidated after Agent-C mutation
- [ ] `pyproject.toml` version `0.7.0`
- [ ] SKILL-B governance checklist passes
