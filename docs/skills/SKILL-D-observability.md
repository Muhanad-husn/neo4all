# SKILL-D: Observability, Logging & Caching

**Applies to**: Every increment, every service, every module.
**Authority**: This skill is referenced from CLAUDE.md Section 17.

---

## Purpose

Establish centralized, consistent logging, caching, and monitoring across the entire application. The user must be able to observe both backend and frontend behavior during runtime. No service operates without structured logging. No expensive read is performed without a cache check.

---

## Part 1: Centralized Logging

### R-D1: Single Logger Factory

All logging flows through `api/observability/logger.py`. No module creates its own logger configuration. The factory produces structlog loggers with:
- **JSON output** in production, **console (key=value)** in development
- Automatic context binding: `run_id`, `phase`, `component`, `request_id`
- Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL

```python
# CORRECT — every module gets its logger from the factory
from api.observability.logger import get_logger
logger = get_logger(__name__)
logger.info("schema_locked", run_id=run_id, version=schema_version)

# BANNED — no ad-hoc logging setup
import logging
logging.basicConfig(...)  # NEVER
```

### R-D2: Correlation IDs

Every request entering FastAPI receives a correlation ID. If a `run_id` is present in the request, it becomes the correlation ID. Otherwise, a request-scoped ID is generated. This ID propagates through:
- All log entries during that request
- All ARQ jobs spawned by that request
- All downstream service calls (graph, vector, storage)

Implemented via FastAPI middleware in `api/observability/middleware.py`.

### R-D3: Structured Log Events

Log entries are structured events, not free-text strings. Each event has:
- `event`: machine-readable event name (e.g., `chunk_ingested`, `proposal_created`, `agent_budget_exceeded`)
- `level`: log level
- `timestamp`: ISO 8601
- `correlation_id`: run_id or request_id
- `component`: module name
- Additional typed fields relevant to the event

```python
# CORRECT — structured event with typed fields
logger.info("extraction_complete", run_id=run_id, chunk_id=chunk_id, nodes=5, edges=3)

# BANNED — unstructured free-text
logger.info(f"Extraction done for chunk {chunk_id}, got 5 nodes and 3 edges")
```

### R-D4: Log Levels Convention

| Level | When to Use |
|-------|-------------|
| DEBUG | Detailed diagnostic info (cache hits, query parameters, intermediate state) |
| INFO | Normal operational events (phase transitions, job start/complete, document ingested) |
| WARNING | Degraded but recoverable (soft-flagged chunks, fallback parser activation, cache miss on expected key) |
| ERROR | Failures requiring attention (LLM malformed output, Neo4j connection failure, hard-rejected chunks) |
| CRITICAL | System-level failures (missing env vars, startup failure, data corruption detected) |

### R-D5: Sensitive Data Exclusion

Logs must never contain:
- Neo4j credentials or connection strings
- OpenRouter API keys
- S3 access keys
- Raw document content (log doc_id and chunk_id, not the text)
- Full LLM prompts or responses in production (DEBUG level only, disabled in prod)

---

## Part 2: Caching

### R-D6: Redis-Backed Cache

The cache layer uses Redis (already in the stack for ARQ). Cache operations are abstracted through `api/cache/client.py` — no direct Redis calls outside this module.

```python
from api.cache.client import CacheClient

cache = CacheClient()

# Typed get/set with TTL
schema = await cache.get("schema", run_id=run_id, model=SchemaVersion)
if schema is None:
    schema = await fetch_schema_from_neo4j(run_id)
    await cache.set("schema", run_id=run_id, value=schema, ttl=3600)
```

### R-D7: Deterministic Cache Keys

Cache keys follow the same deterministic philosophy as artifact IDs. Keys are derived from typed inputs, never constructed by string concatenation.

`api/cache/keys.py` defines key builders:
```python
# CORRECT — deterministic key derivation
key = CacheKey.schema(run_id=run_id)                    # → "schema:{run_id}"
key = CacheKey.manifest(run_id=run_id, doc_id=doc_id)   # → "manifest:{run_id}:{doc_id}"
key = CacheKey.graph_query(run_id=run_id, query_hash=h)  # → "gq:{run_id}:{h}"

# BANNED — ad-hoc string construction
key = f"schema-{run_id}"  # NEVER
```

### R-D8: What to Cache

| Data | Cache Key Pattern | TTL | Invalidation |
|------|------------------|-----|-------------|
| Locked schema | `schema:{run_id}` | Run lifetime (no TTL) | Never — immutable after lock |
| Document manifest | `manifest:{run_id}:{doc_id}` | 1 hour | On re-ingestion |
| Graph reader queries (node counts, type lists) | `gq:{run_id}:{query_hash}` | 5 minutes | On any graph write in that run |
| Chunk text by chunk_id | `chunk:{chunk_id}` | 30 minutes | Never — chunks are immutable |
| Candidate detection results | `candidates:{run_id}:{detection_hash}` | 5 minutes | On graph write or manual regeneration |

### R-D9: Cache Misses Are Not Errors

A cache miss is a normal operational event (logged at DEBUG level). The system falls through to the authoritative source (Neo4j, S3, Qdrant). Cache failures (Redis down) are logged at WARNING and the system continues without caching — never fails closed on a cache miss.

### R-D10: Cache Invalidation on Writes

After any graph mutation (Agent-C execution), invalidate all cache keys scoped to that `run_id` that depend on graph state. This is handled by a post-execution hook in Agent-C, not by individual cache consumers.

---

## Part 3: Monitoring

### R-D11: Backend Monitoring Endpoints

`api/routers/monitoring.py` exposes system observability. Endpoints are introduced progressively:

| Endpoint | Introduced In | Returns |
|----------|--------------|---------|
| `GET /api/monitoring/health` | SPEC-01 | Extended health: per-service connectivity with latency |
| `GET /api/monitoring/logs/recent?limit=100&level=WARNING` | SPEC-01 | Recent log entries, filterable by level and component |
| `GET /api/monitoring/run/{run_id}` | SPEC-01 | Run-level summary: phase, counts, active jobs |
| `GET /api/monitoring/workers` | SPEC-04 | ARQ worker count, active jobs, queue depth |
| `GET /api/monitoring/jobs/{run_id}` | SPEC-04 | Per-job status (queued, running, complete, failed) |
| `GET /api/monitoring/metrics` | SPEC-07 | Aggregated LLM usage: tokens, cost per agent type |
| `GET /api/monitoring/agents/{run_id}` | SPEC-07 | Per-candidate agent chain telemetry |
| `GET /api/monitoring/cache` | SPEC-08 | Cache hit/miss ratio, key count, memory usage |

These endpoints use SKILL-A response models.

### R-D12: Frontend Monitoring Dashboard

`ui/pages/monitoring.py` provides a real-time monitoring view accessible from any phase:

**Backend panel** (polls `/api/monitoring/*` endpoints):
- Service health indicators (green/yellow/red per backend)
- Redis queue depth and worker status
- Recent errors (last 10 WARNING+ log entries)
- Cache performance (hit/miss ratio)
- LLM usage summary (total tokens, estimated cost per agent)

**Frontend panel** (reads from StateManager):
- Current phase and progression
- Active run state (run_id, schema status, document count)
- Ingestion / extraction / curation progress bars
- Time elapsed per phase

**Log viewer**:
- Scrollable, filterable log stream (level, component, time range)
- Auto-refresh toggle (poll interval configurable)
- Up to 100 entries per page, paginated

The monitoring page is **read-only** and accessible from any phase. It does not contain business logic (CLAUDE.md Section 4.1) — it only renders data fetched from backend monitoring endpoints. UI panels gracefully hide sections whose backend endpoints are not yet implemented.

### R-D13: Monitoring Does Not Affect Performance

Monitoring endpoints must not block or slow the primary pipeline:
- Metrics are collected in-memory (counters, gauges) and read on demand — no database queries for metrics
- Log retrieval queries a ring buffer or recent-log store, not a full log scan
- Cache stats come from Redis INFO, not from application-level tracking
- Polling interval in the UI defaults to 5 seconds, configurable

---

## Verification Checklist

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
