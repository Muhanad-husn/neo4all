# SPEC-08: Polish, Observability & Hardening

**Increment**: 8 | **Version target**: 0.8.0 | **Prerequisites**: SPEC-07 complete
**Skills required**: SKILL-A, SKILL-B, SKILL-C, SKILL-D

---

## Objective

Harden the application: dry-run mode, CI pipeline, monitoring polish, logging audit, error handling review, UI polish, and final documentation.

---

## Specifications

### S-08.1: Dry-Run Mode
Add `DRY_RUN` flag to `api/config.py`. When true, Agent-C skips graph mutations. Diffs and proposals generated and logged to S3.

### S-08.2: CI Pipeline
Create `.github/workflows/ci.yml`: ruff, mypy, unit tests, integration tests (skipped without Aura env vars), Docker build.

### S-08.3: Monitoring Polish & Advanced Observability
Update `ui/pages/monitoring.py`: log export (JSON download), cache dashboard (hit/miss chart, key count, memory from `/api/monitoring/cache`), alerting thresholds (error rate, queue depth, cache ratio), historical metrics (per-increment token usage, throughput, errors).
Update `api/routers/monitoring.py`: `GET /api/monitoring/cache` (Redis stats), response time percentiles (p50, p95, p99).

### S-08.4: Logging Hardening
Audit all log calls for SKILL-D compliance. Verify no sensitive data. Verify correlation ID propagation through ARQ chains. Add structured error context to all fail-closed paths.

### S-08.5: Error Handling Audit
Review all endpoints and agent jobs for fail-closed compliance. No silent coercion. Add missing fallback paths.

### S-08.6: UI Polish
Create `ui/pages/dashboard.py`: run summary, graph statistics.
Create `ui/components/phase_indicator.py`: progress indicator widget.
Add audit trail export (JSON) to curation UI.

### S-08.7: ADRs
Create `docs/adr/001-parser-fallback-chain.md`, `docs/adr/002-no-local-neo4j.md`, `docs/adr/003-deterministic-ids.md`.

### S-08.8: Final Documentation
Update `README.md` comprehensively. Update `CLAUDE.md` with any new conventions.

---

## Files to Generate

| # | File Path | Purpose |
|---|-----------|---------|
| 1 | `api/config.py` | Add DRY_RUN (update) |
| 2 | `.github/workflows/ci.yml` | CI pipeline |
| 3 | `api/routers/monitoring.py` | Cache stats, percentiles (update) |
| 4 | `ui/pages/monitoring.py` | Polish: export, cache, alerts (update) |
| 5 | `ui/pages/dashboard.py` | Run summary |
| 6 | `ui/components/phase_indicator.py` | Progress widget |
| 7 | `docs/adr/001-parser-fallback-chain.md` | ADR |
| 8 | `docs/adr/002-no-local-neo4j.md` | ADR |
| 9 | `docs/adr/003-deterministic-ids.md` | ADR |
| 10 | `tests/unit/test_dry_run.py` | Dry-run behavior |
| 11 | `README.md` | Final version |
| 12 | `CLAUDE.md` | Final update |

---

## Acceptance Criteria

- [ ] Dry-run produces proposals/diffs without graph mutations
- [ ] CI passes: lint, type check, unit tests, Docker build
- [ ] `/api/monitoring/cache` returns cache statistics
- [ ] Monitoring UI has log export, cache dashboard, alerting
- [ ] All log calls comply with SKILL-D
- [ ] No sensitive data in any log entry
- [ ] Correlation IDs propagate through all ARQ chains
- [ ] All fail-closed paths verified
- [ ] `pyproject.toml` version `0.8.0`
- [ ] `README.md` and `CLAUDE.md` finalized
- [ ] SKILL-B governance checklist passes
