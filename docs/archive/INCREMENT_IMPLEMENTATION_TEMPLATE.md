# Increment Implementation Template
## For Use with Claude Code

**Purpose**: Structured workflow for implementing each increment (SPEC-01 through SPEC-08).

---

## Pre-Flight Checklist

Before starting ANY increment, verify:

- [ ] Previous increment complete (version bumped, tests passing)
- [ ] Working directory clean (no uncommitted changes)
- [ ] Docker services running (`docker compose up`)
- [ ] New env vars (if any for this increment) added to `.env` and validated
- [ ] Target spec file identified: `docs/specs/SPEC-0X-[name].md`

---

## Phase 1: Context Loading (1 Turn)

```
/start-session
```

Or if `/start-session` is not configured for this increment:

```
Read docs/specs/SPEC-0X-[name].md and all files in docs/skills/.
Ready to implement. What files should I generate next?
```

> **Note**: Do NOT ask Claude to read `CLAUDE.md` — it auto-loads it. Do NOT ask for a briefing summary — proceed directly.

---

## Phase 2: Iterative File Generation (3-8 Turns)

Repeat the prompt below for each batch. Batch sizes:

| Content Type | Max Files |
|-------------|-----------|
| Config, models | 5 |
| Services, routers | 3 |
| Agents, complex logic | 2 |
| Tests | 4 |
| UI pages | 2 |

### Turn Template

```
Generate files [X]-[Y] from SPEC-0X.

[Spec-specific guidance for this batch only, e.g.:]
- File X requires deterministic ID derivation per §5
- File Y must not access st.session_state directly
- File Z needs cache key builder from api/cache/keys.py
```

> **Note**: Do NOT repeat the standing skill rules (SKILL-A through D). Claude already knows them from `CLAUDE.md` §18. Only add guidance **specific to this batch**.

---

## Phase 3: Validation + Governance (1 Turn)

```
Run both checks for SPEC-0X:

1. **Acceptance criteria**: Go through each criterion from the spec's
   "Acceptance Criteria" section. For each, state the criterion, which
   file(s) satisfy it, and confirm compliance or note gaps.

2. **Governance checklist**: Run the SKILL-B post-increment checklist from
   docs/skills/SKILL-B-governance.md. Report PASS/FAIL per item with
   file:line references for any violations. Propose fixes.
```

---

## Phase 4: Testing (1-2 Turns)

```
Generate test files from SPEC-0X's "Files to Generate" table.

Requirements:
- Unit tests: no network, no LLM, no Neo4j. Use fixtures from /fixtures/
- Assert idempotency and fail-closed behavior
- Integration tests: use NEO4J_CI_* env vars, skip if absent
- Follow pytest conventions

Generate tests batch [X-Y].
```

---

## Phase 5: Documentation + Verification + Commit (1 Turn)

```
Complete post-implementation for SPEC-0X:

1. **Documentation**:
   - pyproject.toml: bump version to 0.X.0, verify dependencies with version bounds
   - README.md: mark SPEC-0X complete, add setup/endpoint changes
   - CLAUDE.md: update §2/§9 only if structure or env vars changed

2. **Verification**:
   - Confirm all files from spec's "Files to Generate" table exist
   - Check for unused imports, missing type hints, hardcoded credentials
   - Confirm __init__.py in all new Python directories
   - Verify pyproject.toml entry points

3. **Commit message** (format: `feat: Increment X - [Title]`):
   - List key features and version bump in body
   - List all files created or updated

Show diffs for doc updates, PASS/FAIL for verification, and the commit message.
```

---

## Recovery (If Context Lost)

```
Context reset. Read docs/specs/SPEC-0X-[name].md.
Files 1-[Y] are done. Continue with files [Y+1]-[Z].
```

---

## Quick Reference

- **Total turns**: ~7-12 per increment
- **Standing rules**: CLAUDE.md §18 (skills A-D condensed)
- **Verification checklists**: docs/skills/SKILL-*.md
- **Specs**: docs/specs/SPEC-0X-[name].md

---

**Version**: 2.0
**Last Updated**: 2026-02
**Applies To**: SPEC-01 through SPEC-08
