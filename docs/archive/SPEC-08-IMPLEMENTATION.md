# SPEC-08 Implementation Guide
## Polish, Observability & Hardening

**Increment**: 8 | **Version target**: 0.8.0 | **Prerequisites**: SPEC-07 complete
**Template**: INCREMENT_IMPLEMENTATION_TEMPLATE v2.0

---

## Pre-Flight Checklist

- [ ] SPEC-07 complete (version 0.7.0, tests passing)
- [ ] Working directory clean
- [ ] Docker services running (`docker compose up`)
- [ ] `DRY_RUN` env var ready to add to `.env`
- [ ] Target spec: `docs/specs/SPEC-08-hardening.md`

---

## Phase 1: Context Loading (1 Turn)

```
/start-session
```

Or:

```
Read docs/specs/SPEC-08-hardening.md and all files in docs/skills/.
Ready to implement. What files should I generate next?
```

---

## Phase 2: Iterative File Generation (5 Turns)

### Batch 1 — Dry-Run Mode & CI Pipeline (Files 1, 2) ▸ Turn 2

```
Generate files 1, 2 from SPEC-08.

Guidance:
- File 1 (api/config.py): UPDATE existing. Add DRY_RUN: bool = False to
  settings. When DRY_RUN is True, Agent-C must skip all graph mutations
  (Neo4j writes). Diffs and proposals are still generated and logged
  (structlog event with full diff payload). Add DRY_RUN to env var
  documentation block. Ensure existing config (AgentConfig, cache, etc.)
  is preserved — this is an additive update only.
- File 2 (.github/workflows/ci.yml): CREATE. GitHub Actions CI pipeline:
  Trigger on push to main and pull_request.
  Jobs:
    lint: ruff check . (Python 3.11+)
    typecheck: mypy api/ ui/ (strict mode)
    unit-tests: pytest tests/unit/ -v --tb=short
    integration-tests: pytest tests/integration/ -v --tb=short
      (skip if NEO4J_CI_URI, NEO4J_CI_USER, NEO4J_CI_PASSWORD not set —
      use `if: env.NEO4J_CI_URI != ''`)
    docker-build: docker compose build (verify Dockerfile still works)
  Use ubuntu-latest, Python 3.11, install deps from pyproject.toml.
  Cache pip dependencies. Redis service container for cache tests.
```

### Batch 2 — Monitoring Polish (Files 3, 4) ▸ Turn 3

```
Generate files 3, 4 from SPEC-08.

Guidance:
- File 3 (api/routers/monitoring.py): UPDATE existing. Add:
  GET /api/monitoring/cache — returns Redis stats: total keys, memory usage,
    hit/miss counts, hit ratio. Pydantic response model (CacheStatsResponse).
    Reads from Redis INFO command and application-level hit/miss counters.
  Update existing endpoints to include response time percentiles:
    p50, p95, p99 from in-memory metrics. Pydantic response model update.
  All endpoints: SKILL-A compliant (Pydantic in/out), SKILL-D compliant
    (structured logging, correlation ID).
- File 4 (ui/pages/monitoring.py): UPDATE existing. Add:
  Log export: button to download all structured logs as JSON file.
  Cache dashboard panel: hit/miss ratio chart (st.bar_chart or similar),
    key count, memory usage — data from GET /api/monitoring/cache.
  Alerting thresholds panel: configurable thresholds for error rate,
    queue depth, cache hit ratio — display warning badges when exceeded.
  Historical metrics panel: per-increment token usage, throughput, errors
    over time.
  All via StateManager — no direct st.session_state.
  No business logic — all data from API endpoints. Read-only display.
```

### Batch 3 — UI Polish (Files 5, 6) ▸ Turn 4

```
Generate files 5, 6 from SPEC-08.

Guidance:
- File 5 (ui/pages/dashboard.py): CREATE. Run summary dashboard:
  Current phase indicator (reuses phase_indicator component).
  Run summary: total documents ingested, entities extracted,
    candidates generated, proposals created, proposals approved/rejected.
  Graph statistics: node count by type, relationship count by type —
    data from existing /api/graph/ endpoints or new summary endpoint.
  Recent activity feed: last N actions with timestamps.
  All via StateManager — no direct st.session_state.
  No business logic — display only, all data from API.
- File 6 (ui/components/phase_indicator.py): CREATE. Reusable progress
  indicator widget. Shows all phases (Ingest → Extract → Curate → Review)
  with current phase highlighted. Accepts current_phase as parameter.
  Pure display component — no API calls, no business logic.
  Returns Streamlit elements (st.columns, st.markdown with styled badges).
```

### Batch 4 — ADRs (Files 7, 8, 9) ▸ Turn 5

```
Generate files 7, 8, 9 from SPEC-08.

Guidance:
- File 7 (docs/adr/001-parser-fallback-chain.md): ADR format:
  Title, Status (Accepted), Context (why three-tier parsing),
  Decision (Docling → Unstructured → raw fallback),
  Consequences (resilience vs complexity, quality flags on fallback).
  Reference SPEC-03 ingestion specs.
- File 8 (docs/adr/002-no-local-neo4j.md): ADR format:
  Title, Status (Accepted), Context (why no local Neo4j),
  Decision (Neo4j Aura only — cloud-hosted graph DB),
  Consequences (no offline dev, requires Aura credentials,
  integration tests gated on CI env vars).
  Reference CLAUDE.md architecture decisions.
- File 9 (docs/adr/003-deterministic-ids.md): ADR format:
  Title, Status (Accepted), Context (why deterministic IDs over uuid4()),
  Decision (all entity IDs derived from content hashes — SHA-256 of
  stable inputs: run_id + source + content for docs, doc_id + index for
  chunks, etc.), Consequences (idempotent reruns, deduplication,
  testability, no uuid4() anywhere).
  Reference CLAUDE.md §5/§6.
```

### Batch 5 — Logging & Error Handling Audit (Audit Turn) ▸ Turn 6

```
Perform the S-08.4 and S-08.5 audits from SPEC-08.

S-08.4 Logging Hardening:
- Audit all log calls across api/ for SKILL-D compliance.
- Verify no sensitive data logged (API keys, credentials, raw doc content).
- Verify correlation ID propagation through all ARQ job chains
  (evidence_assembly_job → retrieval_augmentation_job → proposal_composition_job).
- Add structured error context to any fail-closed paths missing it.
- Report: list each file audited, violations found, fixes applied.

S-08.5 Error Handling Audit:
- Review all FastAPI endpoints in api/routers/ for fail-closed compliance.
- Review all ARQ jobs in api/worker/jobs.py.
- Review all agent code in api/agents/.
- Verify: no silent coercion (e.g., empty string defaults hiding errors),
  no bare except clauses, all error paths log structured events.
- Add missing fallback paths where needed.
- Report: list each file audited, violations found, fixes applied.

Also add audit trail export (JSON download) to ui/pages/curation.py
(S-08.6 remaining item).

Output a summary of all changes made with file:line references.
```

---

## Phase 3: Validation + Governance (1 Turn)

```
Run both checks for SPEC-08:

1. **Acceptance criteria**: Go through each criterion from SPEC-08's
   "Acceptance Criteria" section. For each, state the criterion, which
   file(s) satisfy it, and confirm compliance or note gaps.

   Criteria to verify:
   - Dry-run produces proposals/diffs without graph mutations
   - CI passes: lint, type check, unit tests, Docker build
   - /api/monitoring/cache returns cache statistics
   - Monitoring UI has log export, cache dashboard, alerting
   - All log calls comply with SKILL-D
   - No sensitive data in any log entry
   - Correlation IDs propagate through all ARQ chains
   - All fail-closed paths verified
   - pyproject.toml version 0.8.0
   - README.md and CLAUDE.md finalized
   - SKILL-B governance checklist passes

2. **Governance checklist**: Run the SKILL-B post-increment checklist from
   docs/skills/SKILL-B-governance.md. Report PASS/FAIL per item with
   file:line references for any violations. Propose fixes.
```

---

## Phase 4: Testing (1 Turn)

### Test Batch (File 10) ▸ Turn 8

```
Generate test file 10 from SPEC-08.

File:
- tests/unit/test_dry_run.py — Dry-run mode behavior:
  test_dry_run_flag_default: DRY_RUN defaults to False in config.
  test_dry_run_skips_mutations: When DRY_RUN=True, Agent-C graph mutation
    function is not called (mock Neo4j client, verify no write calls).
  test_dry_run_generates_diffs: When DRY_RUN=True, diffs and proposals
    are still generated (verify diff builder and proposal service called).
  test_dry_run_logs_proposals: When DRY_RUN=True, proposals logged via
    structlog (verify log event emitted with proposal payload).
  test_cache_stats_endpoint: /api/monitoring/cache returns valid
    CacheStatsResponse with expected fields (mock Redis INFO).
  test_cache_stats_hit_ratio: Hit ratio calculated correctly from
    hit/miss counters.

Requirements:
- No network, no LLM, no Neo4j. Mock all external services.
- Assert fail-closed behavior on malformed inputs.
- Follow pytest conventions.
- Use fixtures from /fixtures/ where applicable.
```

---

## Phase 5: Documentation + Verification + Commit (1 Turn)

```
Complete post-implementation for SPEC-08:

1. **Documentation**:
   - pyproject.toml: bump version to 0.8.0, verify all dependencies have
     version bounds, verify entry points
   - README.md: FINAL VERSION. Mark all SPEC-01 through SPEC-08 complete.
     Comprehensive documentation:
       - Project overview and architecture
       - Setup instructions (Docker, .env, Neo4j Aura)
       - All API endpoints (grouped by domain)
       - UI pages and navigation
       - CI pipeline usage
       - Dry-run mode documentation
       - Environment variables reference (complete list)
       - Development workflow
   - CLAUDE.md: FINAL UPDATE. Add DRY_RUN to §9 env vars. Update §2 if
     any new directories (docs/adr/, .github/workflows/). Verify all
     sections accurate for v0.8.0 state.

2. **Verification**:
   - Confirm all 12 files from SPEC-08 "Files to Generate" table exist
   - Check for unused imports, missing type hints, hardcoded credentials
   - Confirm __init__.py in all Python directories
   - Verify no uuid4() anywhere in codebase
   - Verify no ad-hoc loggers (all use centralized structlog)
   - Verify no direct st.session_state access in UI
   - Verify no business logic in UI pages
   - Verify pyproject.toml entry points
   - Verify all ADRs follow consistent format

3. **Commit message** (format: feat: Increment 8 - Polish, Observability & Hardening):
   - List key features and version bump in body
   - List all files created or updated
   - Note: this is the final increment

Show diffs for doc updates, PASS/FAIL for verification, and the commit message.
```

---

## Recovery (If Context Lost)

```
Context reset. Read docs/specs/SPEC-08-hardening.md.
Files 1-[Y] are done. Continue with files [Y+1]-[Z].
```

---

## Quick Reference

| Metric | Value |
|--------|-------|
| Total files | 12 (9 source + 1 test + 2 documentation) |
| Total turns | ~10 |
| New files | .github/workflows/ci.yml, ui/pages/dashboard.py, ui/components/phase_indicator.py, 3 ADRs, tests/unit/test_dry_run.py |
| Updated files | api/config.py, api/routers/monitoring.py, ui/pages/monitoring.py, ui/pages/curation.py, README.md, CLAUDE.md |
| Key risk areas | Dry-run must truly skip mutations (not just log), CI must work without Aura creds, logging audit must be thorough (no sensitive data leaks), README must be comprehensive (final version) |
| Special notes | This is the final increment. Batch 5 is an audit turn (no new files, but modifications across codebase). README and CLAUDE.md are final versions — extra care required. |

---

**Version**: 2.0
**Last Updated**: 2026-02
**Template**: INCREMENT_IMPLEMENTATION_TEMPLATE v2.0
