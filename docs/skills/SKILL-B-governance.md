# SKILL-B: Governance — Verification Checklist

**Full rules**: See CLAUDE.md §18 (SKILL-B section)

## Post-Increment Governance Checklist

Run after completing every increment:

- [ ] All new files are in correct directories per CLAUDE.md §2
- [ ] No `st.session_state` access outside `StateManager`
- [ ] No `uuid4()` for governed artifact IDs
- [ ] No business logic in `ui/`
- [ ] No direct graph writes outside the proposal pipeline (except Phase 3 extraction)
- [ ] No layer violations (check imports)
- [ ] All new endpoints have Pydantic request/response models (SKILL-A)
- [ ] All modules use centralized logger (SKILL-D)
- [ ] Cache used for expensive reads (SKILL-D)
- [ ] `README.md` updated with current status
- [ ] `pyproject.toml` version bumped (MINOR for increment completion)
- [ ] CLAUDE.md updated if structure, env vars, or conventions changed
- [ ] All unit tests pass
- [ ] No modules exceed ~400 lines without a refactoring plan

## Folder Placement Reference

| Content Type | Directory |
|---|---|
| UI components | `ui/` |
| Route handlers | `api/routers/` |
| Business logic | `api/services/` |
| Agent code | `api/agents/` |
| Graph interaction | `api/graph/` |
| Vector retrieval | `api/vector/` |
| Storage operations | `api/storage/` |
| Worker jobs | `api/worker/` |
| Schema models | `api/schema/` |
| Proposal models | `api/proposals/` |
| Diff builder | `api/diff/` |
| Audit writers | `api/audit/` |
| Cache layer | `api/cache/` |
| Observability | `api/observability/` |
| Prompt templates | `prompts/` |
| Test fixtures | `fixtures/` |
| Unit tests | `tests/unit/` |
| Integration tests | `tests/integration/` |
| Specifications | `docs/specs/` |
| Skills | `docs/skills/` |
| ADRs | `docs/adr/` |
| Infrastructure | `infra/` (read-only) |
