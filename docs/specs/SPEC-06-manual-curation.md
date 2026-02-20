# SPEC-06: Manual Curation, Evidence Retrieval & Proposal Pipeline (Layer 2)

**Increment**: 6 | **Version target**: 0.6.0 | **Prerequisites**: SPEC-05 complete
**Skills required**: SKILL-A, SKILL-B, SKILL-D

---

## Objective

Implement the full governed mutation pipeline (propose → approve → diff → execute → audit), evidence retrieval from Qdrant for manual decision-making, and a paginated graph explorer for browsing all nodes and edges.

---

## Specifications

### S-06.1: Proposal Packet
Create `api/proposals/models.py`: full model per CLAUDE.md Section 7. Deterministic `proposal_id`.
Create `api/proposals/service.py`: lifecycle — create, list, transition states (pending → approved/rejected/deferred).

### S-06.2: Deterministic Diff Builder
Create `api/diff/models.py`: `DiffPlan`, `DiffOperation` enum (create_node, update_node, delete_node, create_edge, update_edge, delete_edge, merge_nodes).
Create `api/diff/builder.py`: non-LLM, Proposal Packet → structured diff, `diff_id = hash(diff_content)`, same inputs → same diff.

### S-06.3: Approval Gate
Create `api/routers/approvals.py`: `POST .../approve` (issues approval_id), `POST .../reject`, `POST .../defer`. High-risk (merge, delete): two-phase (phase1 → confirm).

### S-06.4: Execution Agent (Agent-C)
Create `api/agents/execution.py`: tools-only, no reasoning. Validates approval_id + schema version. Typed graph mutations. Post-apply invariant checks. Appends immutable audit record. **Post-execution cache invalidation**: invalidates `run_id`-scoped graph cache keys (SKILL-D R-D10).

### S-06.5: Audit Log
Create `api/audit/models.py`: `AuditRecord`.
Create `api/audit/writer.py`: immutable append-only to S3.

### S-06.6: Evidence Retrieval
Create `api/vector/retriever.py`: queries Qdrant by dedupe key, source doc, semantic similarity. Returns ranked chunks with text, source, page locator, relevance score.
Create `api/routers/evidence.py`: `GET /api/curation/evidence/{candidate_id}`, `POST /api/curation/evidence/query` (ad-hoc by node/edge reference).

### S-06.7: Graph Explorer (Paginated)
Create `api/routers/graph_explorer.py`:
- `GET /api/graph/nodes/{run_id}?page=1&page_size=50&node_type=...` — max 50/page, filterable
- `GET /api/graph/edges/{run_id}?page=1&page_size=50&edge_type=...` — same pattern
- `GET /api/graph/nodes/{run_id}/count`, `GET /api/graph/edges/{run_id}/count` — totals by type

### S-06.8: Manual Curation Endpoints
Update `api/routers/curation.py`: `POST /api/curation/propose` (same pipeline as AI — no bypass), `GET /api/curation/proposals/{run_id}`, `POST /api/curation/proposals/{id}/execute`.

### S-06.9: UI
Update `ui/pages/curation.py`: candidate detail with evidence view, proposal form, diff preview, approval queue, execution status, audit trail.
Create `ui/pages/graph_explorer.py`: scrollable tables (50 rows/page), filterable by type, pagination controls. Read-only.

---

## Files to Generate

| # | File Path | Purpose |
|---|-----------|---------|
| 1 | `api/proposals/models.py` | Proposal Packet |
| 2 | `api/proposals/service.py` | Proposal lifecycle |
| 3 | `api/diff/models.py` | Diff models |
| 4 | `api/diff/builder.py` | Deterministic diff builder |
| 5 | `api/audit/models.py` | Audit record |
| 6 | `api/audit/writer.py` | Immutable audit writer |
| 7 | `api/agents/execution.py` | Agent-C |
| 8 | `api/vector/retriever.py` | Evidence retrieval |
| 9 | `api/routers/evidence.py` | Evidence endpoints |
| 10 | `api/routers/approvals.py` | Approval gate |
| 11 | `api/routers/graph_explorer.py` | Paginated graph browse |
| 12 | `api/routers/curation.py` | Manual curation (update) |
| 13 | `ui/pages/curation.py` | Curation UI (update) |
| 14 | `ui/pages/graph_explorer.py` | Graph explorer UI |
| 15 | `tests/unit/test_diff_determinism.py` | Diff reproducibility |
| 16 | `tests/unit/test_proposal_lifecycle.py` | State transitions |
| 17 | `tests/unit/test_approval_gate.py` | Approval validation |
| 18 | `tests/unit/test_audit_record.py` | Audit structure |
| 19 | `tests/unit/test_evidence_retrieval.py` | Evidence queries |
| 20 | `tests/unit/test_graph_explorer_pagination.py` | Pagination |
| 21 | `tests/integration/test_manual_curation_cycle.py` | End-to-end |
| 22 | `fixtures/curation/sample_proposal.json` | Test proposal |
| 23 | `fixtures/curation/sample_diff.json` | Test diff |

---

## Acceptance Criteria

- [ ] Full cycle: candidate → evidence → propose → diff → approve → execute → audit
- [ ] Evidence returns Qdrant chunks with source/page metadata
- [ ] Diff determinism: same proposal → same diff_id
- [ ] Execution rejected without valid approval_id
- [ ] Two-phase approval for merge/delete
- [ ] Graph explorer: paginated 50/page with type filtering
- [ ] Audit records immutable in S3
- [ ] Cache invalidated after Agent-C mutation
- [ ] `pyproject.toml` version `0.6.0`
- [ ] SKILL-B governance checklist passes
