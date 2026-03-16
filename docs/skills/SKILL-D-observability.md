# SKILL-D: Observability — Verification Checklist

**Full rules**: See CLAUDE.md §18 (SKILL-D section)

## Verification Checklist

Run after every increment:

- [ ] All modules use `get_logger(__name__)` from `api/observability/logger.py`
- [ ] No free-text log messages — all events are structured
- [ ] Correlation IDs present in all log entries within a request/job scope
- [ ] No sensitive data in logs (credentials, keys, raw document text)
- [ ] Cache client used for all cacheable reads listed in R-D8
- [ ] Cache keys derived via `CacheKey` builders — no ad-hoc strings
- [ ] Cache misses handled gracefully — fallthrough to source, no errors
- [ ] Cache invalidated after graph mutations
- [ ] Backend monitoring endpoints return structured, typed responses
- [ ] Frontend monitoring page renders backend + frontend state
- [ ] Monitoring page has no business logic — pure data display

## Monitoring Endpoint Table

| Endpoint | Introduced In | Returns |
|----------|--------------|---------| 
| `GET /api/monitoring/health` | SPEC-01 | Extended health: per-service connectivity with latency |
| `GET /api/monitoring/logs/recent` | SPEC-01 | Recent log entries, filterable by level and component |
| `GET /api/monitoring/run/{run_id}` | SPEC-01 | Run-level summary: phase, counts, active jobs |
| `GET /api/monitoring/workers` | SPEC-04 | ARQ worker count, active jobs, queue depth |
| `GET /api/monitoring/jobs/{run_id}` | SPEC-04 | Per-job status (queued, running, complete, failed) |
| `GET /api/monitoring/metrics` | SPEC-07 | Aggregated LLM usage: tokens, cost per agent type |
| `GET /api/monitoring/agents/{run_id}` | SPEC-07 | Per-candidate agent chain telemetry |
| `GET /api/monitoring/cache` | SPEC-08 | Cache hit/miss ratio, key count, memory usage |

## Cache TTL Reference

| Data | Key Pattern | TTL |
|------|------------|-----|
| Locked schema | `schema:{run_id}` | Run lifetime |
| Document manifest | `manifest:{run_id}:{doc_id}` | 1 hour |
| Graph reader queries | `gq:{run_id}:{query_hash}` | 5 minutes |
| Chunk text | `chunk:{chunk_id}` | 30 minutes |
| Candidate results | `candidates:{run_id}:{detection_hash}` | 24 hours |
