# SPEC-01: Project Scaffolding & Session Lifecycle

**Increment**: 1 | **Version target**: 0.1.0 | **Prerequisites**: None
**Skills required**: SKILL-A, SKILL-B, SKILL-C, SKILL-D

---

## Objective

Create the monorepo skeleton, Docker environment, configuration system, session model, StateManager, centralized logging, cache layer, monitoring infrastructure, and minimal FastAPI + Streamlit shells. This increment produces a running, observable application.

---

## Specifications

### S-01.1: Repository Skeleton
Generate the full directory tree per CLAUDE.md Section 2 (including `api/cache/` and `api/observability/`). Every Python directory must contain `__init__.py` (SKILL-C R-C3).

### S-01.2: Project Definition
Create `pyproject.toml`: PEP 621, Python 3.12, all core dependencies with version bounds (FastAPI, uvicorn, Streamlit, Pydantic v2, ARQ, boto3, neo4j driver, qdrant-client, httpx, structlog, redis async, ruff, mypy). Entry points per SKILL-C R-C1. Version: `0.1.0`.

### S-01.3: Docker Environment
Create `docker-compose.yml`: FastAPI, Streamlit, Redis, RustFS, Qdrant. No Neo4j (Aura-only per CLAUDE.md Section 11.2). Create per-service Dockerfiles: `Dockerfile.api`, `Dockerfile.worker`, `Dockerfile.ui` (SKILL-C R-C5).

### S-01.4: Configuration
Create `api/config.py`: Pydantic `Settings` loading all env vars from CLAUDE.md Section 11.3. Includes `LOG_FORMAT` (json|console, default json), `LOG_LEVEL` (default INFO). Fail-closed on missing required vars (CLAUDE.md Section 4.4).

### S-01.5: Session/Run Model
Create `api/models/run.py`: `Run` Pydantic model. Deterministic `run_id = hash(user_namespace + timestamp_seed)` — never `uuid4()`. Fields: run_id, schema_version (initially null), phase, created_at.

### S-01.6: StateManager
Create `ui/state.py`: `Phase` enum (INIT, SCHEMA, INGESTION, EXTRACTION, CURATION). `StateManager` class as sole interface to `st.session_state`. `advance_phase()` enforces strict ordering. Panel read/write mode tracking.

### S-01.7: Streamlit Shell
Create `ui/app.py`: Phase 0 screen (Neo4j URI/creds, OpenRouter key). Sidebar shows run state. All access through StateManager.

### S-01.8: FastAPI Shell
Create `api/main.py`: CORS, register observability middleware, startup hooks (Neo4j, Redis, S3, Qdrant validation), shutdown hooks. Create `api/routers/health.py`: `GET /api/health` with SKILL-A models.

### S-01.9: README
Create `README.md`: project overview, setup instructions, current status.

### S-01.10: Centralized Logging
Create `api/observability/logger.py`: structlog factory (`get_logger(__name__)`), JSON/console controlled by `LOG_FORMAT`.
Create `api/observability/correlation.py`: correlation ID injection (uses `run_id` or generates request-scoped ID).
Create `api/observability/middleware.py`: FastAPI middleware for request logging, correlation ID injection, duration measurement.
Create `api/observability/metrics.py`: in-memory metrics collector (counters, gauges, latency histogram). Thread-safe, no external deps.

### S-01.11: Cache Layer
Create `api/cache/client.py`: `CacheClient` wrapping Redis with typed async `get()`, `set()`, `delete()`, `invalidate_prefix()`. Graceful degradation on Redis failure (SKILL-D R-D9). Uses `REDIS_URL`.
Create `api/cache/keys.py`: `CacheKey` with static methods for deterministic key derivation (SKILL-D R-D7).
Create `api/cache/config.py`: TTL defaults per data type.

### S-01.12: Monitoring Endpoints
Create `api/routers/monitoring.py`:
- `GET /api/monitoring/health` — per-service connectivity with latency
- `GET /api/monitoring/logs/recent?limit=100&level=WARNING` — recent logs from ring buffer
- `GET /api/monitoring/run/{run_id}` — run-level summary
All use SKILL-A response models.

### S-01.13: Monitoring UI
Create `ui/pages/monitoring.py`: service health indicators, recent error viewer, run state summary, frontend phase/timing panel, auto-refresh toggle (5s default). Read-only, no business logic.

---

## Files to Generate

| # | File Path | Purpose |
|---|-----------|---------|
| 1 | `pyproject.toml` | Project definition |
| 2 | `README.md` | Documentation |
| 3 | `docker-compose.yml` | Local dev environment |
| 4 | `Dockerfile.api` | API container |
| 5 | `Dockerfile.worker` | Worker container |
| 6 | `Dockerfile.ui` | UI container |
| 7 | All `__init__.py` files | Package inits per CLAUDE.md Section 2 |
| 8 | `api/config.py` | Configuration |
| 9 | `api/models/run.py` | Run model |
| 10 | `api/observability/logger.py` | Logger factory |
| 11 | `api/observability/correlation.py` | Correlation IDs |
| 12 | `api/observability/middleware.py` | Request middleware |
| 13 | `api/observability/metrics.py` | Metrics collector |
| 14 | `api/cache/client.py` | Cache abstraction |
| 15 | `api/cache/keys.py` | Key builders |
| 16 | `api/cache/config.py` | TTL config |
| 17 | `api/main.py` | FastAPI app |
| 18 | `api/routers/health.py` | Health endpoint |
| 19 | `api/routers/monitoring.py` | Monitoring endpoints |
| 20 | `ui/state.py` | StateManager |
| 21 | `ui/app.py` | Streamlit entry |
| 22 | `ui/pages/monitoring.py` | Monitoring dashboard |
| 23 | `tests/unit/test_run_id.py` | Deterministic ID |
| 24 | `tests/unit/test_state_manager.py` | Phase enforcement |
| 25 | `tests/unit/test_cache_keys.py` | Cache key determinism |
| 26 | `tests/unit/test_logger.py` | Logger output format |
| 27 | `tests/unit/test_metrics.py` | Metrics correctness |

---

## Acceptance Criteria

- [ ] `docker compose up` starts all services (given valid env vars)
- [ ] `GET /api/health` returns per-backend connectivity status
- [ ] StateManager rejects phase skipping
- [ ] `run_id` deterministic: same inputs → same output
- [ ] Missing required env var → startup failure with clear error
- [ ] All services use centralized logger — no ad-hoc logging
- [ ] Correlation IDs in all log entries within a request
- [ ] Cache client connects to Redis; miss falls through gracefully
- [ ] Cache keys deterministic: same inputs → same key
- [ ] Redis down → WARNING logged, app continues
- [ ] `/api/monitoring/health` returns per-service latency
- [ ] Monitoring UI shows health and recent errors
- [ ] `LOG_FORMAT` and `LOG_LEVEL` functional
- [ ] `pyproject.toml` version `0.1.0`
- [ ] SKILL-B governance checklist passes
