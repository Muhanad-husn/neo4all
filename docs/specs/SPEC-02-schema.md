# SPEC-02: Domain Schema Definition (Phase 1)

**Increment**: 2 | **Version target**: 0.2.0 | **Prerequisites**: SPEC-01 complete
**Skills required**: SKILL-A, SKILL-B, SKILL-D

---

## Objective

Implement Phase 1: user describes a domain, AI proposes node/edge types, user reviews/edits and approves, schema locks for the run.

---

## Specifications

### S-02.1: Schema Models
Create `api/schema/models.py`: `NodeTypeDef` (class, type, primary property, qualifier, additional properties), `EdgeTypeDef` (start/end node types, type, primary property, qualifier, additional properties), `SchemaVersion` (immutable wrapper, deterministic version hash from sorted canonical representation). Frozen post-approval.

### S-02.2: Schema Service
Create `api/schema/service.py`: `propose()` calls OpenRouter, `approve()` locks schema, `get_current()` returns locked version or None. Post-lock → `SchemaLockedError`. Cache locked schema via `CacheKey.schema(run_id)` — immutable, no TTL (SKILL-D R-D8).

### S-02.3: LLM Client
Create `api/services/llm.py`: typed OpenRouter client, model-per-job config, response validation against Pydantic model, fail-closed on malformed output with structured fallback.

### S-02.4: Endpoints
Create `api/routers/schema.py`: `POST /api/schema/propose`, `POST /api/schema/approve`, `GET /api/schema/{run_id}`. All use SKILL-A models.

### S-02.5: Prompt Template
Create `prompts/schema_propose/v1.yaml`: job_id `schema_propose`, template_version `v1`, system prompt, user template with `{domain_description}`, output schema reference. No inline prompts.

### S-02.6: UI
Create `ui/pages/schema.py`: Phase 1 panel — domain description input, proposal trigger, editable node/edge type tables, approve button → lock → advance phase.

---

## Files to Generate

| # | File Path | Purpose |
|---|-----------|---------|
| 1 | `api/schema/models.py` | Schema Pydantic models |
| 2 | `api/schema/service.py` | Schema lifecycle |
| 3 | `api/services/llm.py` | OpenRouter client |
| 4 | `api/routers/schema.py` | Schema endpoints |
| 5 | `prompts/schema_propose/v1.yaml` | Prompt template |
| 6 | `ui/pages/schema.py` | Phase 1 UI |
| 7 | `tests/unit/test_schema_models.py` | Version hash determinism |
| 8 | `tests/unit/test_schema_lock.py` | Post-lock rejection |
| 9 | `tests/unit/test_llm_validation.py` | Malformed response handling |

---

## Acceptance Criteria

- [ ] `POST /api/schema/propose` returns typed candidates
- [ ] `POST /api/schema/approve` locks schema; modifications rejected
- [ ] Schema version hash deterministic
- [ ] Locked schema cached — repeated reads skip Neo4j
- [ ] Malformed LLM response → fail-closed fallback
- [ ] Prompt loaded from template file — no inline strings
- [ ] UI enforces phase ordering via StateManager
- [ ] `pyproject.toml` version `0.2.0`
- [ ] SKILL-B governance checklist passes
