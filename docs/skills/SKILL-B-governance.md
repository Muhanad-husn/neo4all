# SKILL-B: Memory & Architecture Governance

**Applies to**: Every increment, every file change, every PR.
**Authority**: This skill is referenced from CLAUDE.md Section 17.

---

## Purpose

Ensure Claude Code remains consistent with long-term architectural guidelines. This skill prompts refactoring, enforces folder structure, maintains documentation, and applies semantic versioning.

---

## Rules

### R-B1: CLAUDE.md Is the Constitution

Before generating or modifying any file, verify the change does not violate:
- CLAUDE.md Section 4 (Architecture Rules — non-negotiable)
- CLAUDE.md Section 5 (Session & Terminology)
- CLAUDE.md Section 6 (Identity & ID Conventions)
- CLAUDE.md Section 10 (UI State Machine)

If a proposed change conflicts with CLAUDE.md, the change is wrong — not CLAUDE.md.

### R-B2: Folder Structure Is Enforced

Every new file must land in the correct directory per CLAUDE.md Section 2:

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

A file in the wrong directory is a blocking issue.

### R-B3: Layer Violations Are Blockers

These imports are forbidden and must never appear:
- `ui/` importing from `api/graph/`, `api/agents/`, `api/vector/`, `api/diff/`, or `api/audit/`
- `api/vector/` or `api/agents/` (except Agent-C) importing write functions from `api/graph/`
- Any module importing `st.session_state` outside of `ui/state.py`

If Claude Code detects a layer violation during implementation, it must refactor before proceeding.

### R-B4: Documentation Updates Are Mandatory

After any increment that introduces:
- **A new module or directory** → Update CLAUDE.md Section 2 if the tree has changed
- **A new environment variable** → Add to CLAUDE.md Section 11.3
- **A new agent or pipeline component** → Update CLAUDE.md Section 9
- **A changed architectural decision** → Create an ADR in `docs/adr/NNN-title.md`

### R-B5: README.md Maintenance

`README.md` at the repo root must be updated at the end of every increment with:
- Current project status (which increments are complete)
- Setup instructions (reflecting current docker-compose and env vars)
- Available API endpoints (summary with link to `/docs`)
- Known limitations

### R-B6: Semantic Versioning

The `version` field in `pyproject.toml` follows semver:
- **PATCH** (0.x.Y): Bug fixes within an increment
- **MINOR** (0.X.0): Each completed increment (Increment 1 → 0.1.0, Increment 2 → 0.2.0, ...)
- **MAJOR** (X.0.0): Breaking change to API contracts or data model that invalidates existing runs

### R-B7: Refactoring Triggers

Claude Code must proactively refactor when:
- A module exceeds ~400 lines → split into focused sub-modules
- A function takes more than 5 parameters → introduce a params Pydantic model
- Duplicated logic appears in 2+ modules → extract to `api/common/` or shared service
- A Pydantic model is used across 3+ modules → move to `api/models/`

Refactoring is not optional polish — it is a governance requirement.

### R-B8: CLAUDE.md Updates for Spec/Skill References

When a new spec or skill document is created, CLAUDE.md Section 17 must be updated to reference it. Claude Code discovers which spec to follow by reading CLAUDE.md first.

---

## Post-Increment Governance Checklist

Run this after completing every increment:

- [ ] All new files are in correct directories per CLAUDE.md Section 2
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

---

## Claude Code Procedure

At the start of every task:
1. Read CLAUDE.md
2. Identify which increment spec applies (Section 17 references)
3. Read the relevant `docs/specs/SPEC-*.md`
4. Read ALL skill files in `docs/skills/`
5. Verify the planned changes against R-B1 through R-B8
6. Implement
7. Run the post-increment governance checklist
8. Update README.md and CLAUDE.md as required
