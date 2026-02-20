# Supplement: Centralized Logging, Caching & Monitoring

**Purpose**: This supplement adds three foundational systems to the governance artifact plan. It introduces a new skill (SKILL-D), specifies updates to existing specs (SPEC-01, SPEC-04, SPEC-07, SPEC-08), adds new directories to the repo structure, and details all required CLAUDE.md updates.

Apply this supplement **after** generating the base governance artifacts, or integrate it during initial generation.

---

## Architectural Placement

### Why These Are Foundational

Logging, caching, and monitoring are **not polish** — they are infrastructure that every subsequent component depends on. Deferring them to Increment 8 means 7 increments run blind. The correct placement:

| System | When Introduced | Why |
|--------|----------------|-----|
| **Centralized Logging** | Increment 1 (SPEC-01) | Every service, endpoint, and agent needs structured logging from day one |
| **Cache Layer** | Increment 1 (SPEC-01) | Schema lookups, manifest checks, and graph reader queries all benefit from caching early |
| **Monitoring Endpoints** | Increment 1 (SPEC-01) | Backend health monitoring extends naturally from the health check |
| **Monitoring UI** | Increment 1 (SPEC-01) | User needs visibility into system state from the first run |
| **Worker Monitoring** | Increment 4 (SPEC-04) | First use of ARQ — job queue depth, worker status become relevant |
| **Agent Telemetry** | Increment 7 (SPEC-07) | Per-agent token usage, cost tracking, evidence scores |
| **Monitoring Polish** | Increment 8 (SPEC-08) | Advanced dashboards, log export, alerting thresholds |

### New Directories

Two new modules are added under `api/`:

```
api/
├── ...existing modules...
├── cache/               # Redis-backed cache abstraction layer
│   ├── __init__.py
│   ├── client.py        # Cache client with typed get/set/invalidate/ttl
│   ├── keys.py          # Deterministic cache key derivation
│   └── config.py        # Cache TTL and eviction policy configuration
└── observability/       # Centralized logging, metrics, and tracing
    ├── __init__.py
    ├── logger.py         # structlog factory — single logger configuration for all services
    ├── correlation.py    # Correlation ID middleware (run_id propagation)
    ├── metrics.py        # Lightweight metrics collector (counters, timers, gauges)
    └── middleware.py     # FastAPI middleware for request logging and timing
```

And one new UI page:

```
ui/
├── ...existing pages...
└── pages/
    └── monitoring.py    # Real-time monitoring dashboard for both backend and frontend
```

---

## New Artifact: SKILL-D — Observability, Logging & Caching

### File: `docs/skills/SKILL-D-observability.md`

```markdown
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
key = CacheKey.schema(run_id=run_id)           # → "schema:{run_id}"
key = CacheKey.manifest(run_id=run_id, doc_id=doc_id)  # → "manifest:{run_id}:{doc_id}"
key = CacheKey.graph_query(run_id=run_id, query_hash=h) # → "gq:{run_id}:{h}"

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

`api/routers/monitoring.py` exposes system observability:

| Endpoint | Returns |
|----------|---------|
| `GET /api/monitoring/health` | Extended health: per-service connectivity (Neo4j, Redis, S3, Qdrant) with latency |
| `GET /api/monitoring/workers` | ARQ worker count, active jobs, queue depth per job type |
| `GET /api/monitoring/jobs/{run_id}` | Job status for all jobs in a run (queued, running, complete, failed) |
| `GET /api/monitoring/cache` | Cache hit/miss ratio, key count, memory usage |
| `GET /api/monitoring/metrics` | Aggregated metrics: request count, error rate, avg latency, LLM token usage |
| `GET /api/monitoring/logs/recent?limit=100&level=WARNING` | Recent structured log entries, filterable by level and component |
| `GET /api/monitoring/run/{run_id}` | Run-level summary: current phase, document count, chunk count, node/edge count, proposal count, active jobs |

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

The monitoring page is **read-only** and accessible from any phase. It does not contain business logic (CLAUDE.md Section 4.1) — it only renders data fetched from backend monitoring endpoints.

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
```

---

## Updates to Existing Specs

### SPEC-01 Additions (Scaffolding — Increment 1)

Add the following sections to SPEC-01:

```markdown
### S-01.10: Centralized Logging

Create `api/observability/logger.py`:
- structlog-based logger factory
- JSON output (prod), console output (dev), controlled by env var `LOG_FORMAT=json|console`
- Default context fields: timestamp, level, component

Create `api/observability/correlation.py`:
- Correlation ID injection — uses `run_id` when available, generates request-scoped ID otherwise

Create `api/observability/middleware.py`:
- FastAPI middleware: logs request start/end, injects correlation ID into context, measures request duration

Add `LOG_FORMAT` and `LOG_LEVEL` to `api/config.py` (and CLAUDE.md Section 11.3).

### S-01.11: Cache Layer

Create `api/cache/client.py`:
- `CacheClient` class wrapping Redis with typed async `get()`, `set()`, `delete()`, `invalidate_prefix()`
- Graceful degradation: Redis failure → log WARNING, continue without cache
- Connection uses existing `REDIS_URL` env var

Create `api/cache/keys.py`:
- `CacheKey` class with static methods for deterministic key derivation per data type

Create `api/cache/config.py`:
- TTL defaults per data type (configurable via env vars or config file)

### S-01.12: Monitoring Endpoints

Create `api/routers/monitoring.py`:
- `GET /api/monitoring/health` — extended health with per-service latency
- `GET /api/monitoring/logs/recent` — recent log entries from in-memory ring buffer
- `GET /api/monitoring/run/{run_id}` — run-level summary

Create `api/observability/metrics.py`:
- In-memory metrics collector (request count, error count, latency histogram)
- Thread-safe counters and gauges — no external dependency

### S-01.13: Monitoring UI

Create `ui/pages/monitoring.py`:
- Service health indicators (polls `/api/monitoring/health`)
- Recent error log viewer (polls `/api/monitoring/logs/recent`)
- Current run state summary (from StateManager + `/api/monitoring/run/{run_id}`)
- Auto-refresh toggle (default 5s)
- Accessible from all phases — read-only, no business logic
```

Add to SPEC-01 **Files to Generate** table:

```
| 16 | api/observability/__init__.py        | Package init                  |
| 17 | api/observability/logger.py          | Centralized logger factory    |
| 18 | api/observability/correlation.py     | Correlation ID management     |
| 19 | api/observability/middleware.py       | Request logging middleware     |
| 20 | api/observability/metrics.py         | In-memory metrics collector   |
| 21 | api/cache/__init__.py                | Package init                  |
| 22 | api/cache/client.py                  | Redis cache abstraction       |
| 23 | api/cache/keys.py                    | Deterministic key builders    |
| 24 | api/cache/config.py                  | TTL and eviction config       |
| 25 | api/routers/monitoring.py            | Monitoring endpoints          |
| 26 | ui/pages/monitoring.py               | Monitoring dashboard          |
| 27 | tests/unit/test_cache_keys.py        | Cache key determinism         |
| 28 | tests/unit/test_logger.py            | Logger factory output format  |
| 29 | tests/unit/test_metrics.py           | Metrics counter correctness   |
```

Add to SPEC-01 **Acceptance Criteria**:

```
- [ ] All services use centralized logger — no ad-hoc logging
- [ ] Correlation IDs appear in all log entries within a request
- [ ] Cache client connects to Redis; cache miss falls through gracefully
- [ ] Cache keys are deterministic: same inputs → same key
- [ ] Redis down → WARNING logged, app continues without cache
- [ ] `/api/monitoring/health` returns per-service connectivity with latency
- [ ] Monitoring UI shows service health and recent errors
- [ ] `LOG_FORMAT` and `LOG_LEVEL` env vars added to config
```

---

### SPEC-04 Additions (Extraction — Increment 4)

Add to SPEC-04 after S-04.6:

```markdown
### S-04.8: Worker Monitoring

Update `api/routers/monitoring.py`:
- `GET /api/monitoring/workers` — ARQ worker count, active jobs, queue depth per job type
- `GET /api/monitoring/jobs/{run_id}` — per-job status (queued, running, complete, failed)

Update `ui/pages/monitoring.py`:
- Worker status panel: active workers, queue depth, job throughput
- Per-run job tracker: extraction job progress table

### S-04.9: Extraction Caching

Use cache layer for:
- Schema lookups during extraction (`CacheKey.schema(run_id)`) — locked schema is immutable, cache indefinitely per run
- Chunk text retrieval when building extraction context
```

Add to SPEC-04 **Files to Generate** table:

```
| 14 | api/routers/monitoring.py  | Worker monitoring endpoints (update) |
| 15 | ui/pages/monitoring.py     | Worker monitoring panel (update)     |
```

Add to SPEC-04 **Acceptance Criteria**:

```
- [ ] `/api/monitoring/workers` returns queue depth and active job count
- [ ] `/api/monitoring/jobs/{run_id}` shows per-chunk extraction job status
- [ ] Schema cached during extraction — no repeated Neo4j lookups per chunk
- [ ] Monitoring UI shows extraction job progress
```

---

### SPEC-07 Additions (Agent Pipeline — Increment 7)

Add to SPEC-07 after S-07.8:

```markdown
### S-07.10: Agent Telemetry

Update `api/observability/metrics.py`:
- Per-agent metrics: token usage (input/output), cost estimate, execution time, evidence score
- Metrics keyed by `(run_id, candidate_id, agent_name)`

Update `api/routers/monitoring.py`:
- `GET /api/monitoring/metrics` — aggregated LLM usage (total tokens, cost per agent type)
- `GET /api/monitoring/agents/{run_id}` — per-candidate agent chain telemetry

Update `ui/pages/monitoring.py`:
- Agent pipeline panel: per-agent token usage, cost, timing
- Budget consumption gauges per candidate

### S-07.11: Agent Pipeline Caching

Use cache layer for:
- Graph context assembled by Agent-A (`CacheKey.graph_query(run_id, query_hash)`)
- Chunk text retrieved for evidence (`CacheKey.chunk(chunk_id)`)
- Cache invalidated after any Agent-C execution via post-execution hook
```

Add to SPEC-07 **Acceptance Criteria**:

```
- [ ] Agent telemetry metrics recorded for each agent execution
- [ ] `/api/monitoring/agents/{run_id}` returns per-candidate pipeline telemetry
- [ ] Monitoring UI shows per-agent token usage and cost
- [ ] Graph context and chunk text cached during agent pipeline
- [ ] Cache invalidated after Agent-C executes a graph mutation
```

---

### SPEC-08 Revisions (Hardening — Increment 8)

**Remove** S-08.3 (Structured Logging) from SPEC-08 — it is now in SPEC-01.

**Replace** with:

```markdown
### S-08.3: Monitoring Polish & Advanced Observability

Update `ui/pages/monitoring.py`:
- Log export: download filtered log entries as JSON
- Cache dashboard: hit/miss ratio chart, key count, memory usage (from `/api/monitoring/cache`)
- Alerting thresholds: visual warnings when error rate exceeds threshold, queue depth exceeds limit, or cache hit ratio drops below threshold
- Historical metrics: per-increment summary of token usage, job throughput, and error rates

Update `api/routers/monitoring.py`:
- `GET /api/monitoring/cache` — Redis cache stats (hit/miss ratio, key count, memory)
- Response time percentiles (p50, p95, p99) for all endpoint groups

### S-08.4: Logging Hardening

- Audit all log calls across all modules for SKILL-D compliance
- Verify no sensitive data in any log entry
- Verify correlation IDs propagate through all ARQ job chains
- Add structured error context to all fail-closed paths (error code, affected artifact IDs, recovery hint)
```

---

## CLAUDE.md Updates

### Update Section 2 (Repository Structure)

Add `cache/` and `observability/` to the `api/` tree:

```
api/
│   ├── routers/         # HTTP route handlers
│   ├── services/        # Domain services (ingestion, extraction, curation)
│   ├── agents/          # Agent components (typed, constrained, not autonomous)
│   ├── graph/           # Neo4j interaction layer (sole write authority)
│   ├── vector/          # Qdrant retrieval (evidence-only, no write authority)
│   ├── storage/         # Artifact storage via boto3 (S3-compatible)
│   ├── worker/          # ARQ worker entry point and job definitions
│   ├── schema/          # Domain schema definitions and versioning
│   ├── proposals/       # Proposal Packet models and lifecycle
│   ├── diff/            # Deterministic diff builder (non-LLM)
│   ├── audit/           # Immutable audit log writers
│   ├── cache/           # Redis-backed cache abstraction layer
│   └── observability/   # Centralized logging, metrics, correlation IDs
```

### Update Section 3 (Tech Stack)

Add a row:

```
| Caching          | Redis (shared with ARQ)  |
| Observability    | structlog + in-memory metrics |
```

### Update Section 11.3 (Environment Variables)

Add:

```bash
# Observability
LOG_FORMAT                 # json (production) or console (development)
LOG_LEVEL                  # DEBUG, INFO, WARNING, ERROR — default INFO
```

### Update Section 17.3 (Cross-Cutting Skills)

Add SKILL-D to the table:

```
| [SKILL-D](docs/skills/SKILL-D-observability.md) | Every module | Centralized logging, caching, and monitoring |
```

### Update Section 18 (What Claude Should Always Do)

Add:

```markdown
- Use the centralized logger from `api/observability/logger.py` — never create ad-hoc loggers
- Check the cache before expensive reads — use `api/cache/client.py` with deterministic keys
- Log structured events with correlation IDs — never free-text log messages
```

### Update Section 19 (What Claude Must Never Do)

Add:

```markdown
- Create ad-hoc loggers or use Python's `logging.basicConfig()` directly
- Construct cache keys by string concatenation — use `CacheKey` builders
- Log credentials, API keys, or raw document content
- Perform expensive reads without checking the cache first
- Add business logic to the monitoring UI page
```

---

## Updated Execution Summary

Add these steps to the governance artifact generation sequence:

| Step | Action | Files Created/Modified |
|------|--------|----------------------|
| 4.1 | Generate SKILL-D | `docs/skills/SKILL-D-observability.md` |
| 5.1 | Update SPEC-01 | Add S-01.10 through S-01.13 + file table + acceptance criteria |
| 8.1 | Update SPEC-04 | Add S-04.8, S-04.9 + file table + acceptance criteria |
| 11.1 | Update SPEC-07 | Add S-07.10, S-07.11 + acceptance criteria |
| 12.1 | Update SPEC-08 | Replace S-08.3, add S-08.4 |
| 13–15 | Update CLAUDE.md | Sections 2, 3, 11.3, 17.3, 18, 19 |

---

## Impact Summary

| Spec | What Changes |
|------|-------------|
| **SPEC-01** | +4 new specification sections (S-01.10–13), +14 new files, +8 acceptance criteria |
| **SPEC-04** | +2 new sections (S-04.8–9), +2 file updates, +4 acceptance criteria |
| **SPEC-07** | +2 new sections (S-07.10–11), +5 acceptance criteria |
| **SPEC-08** | S-08.3 replaced (logging → monitoring polish), +S-08.4 (logging hardening) |
| **SKILL-D** | New skill document — 13 rules across logging, caching, monitoring |
| **CLAUDE.md** | Sections 2, 3, 11.3, 17.3, 18, 19 updated |

No new increments are created. No increment ordering changes. The existing 8-increment sequence absorbs these systems at the architecturally correct points.
