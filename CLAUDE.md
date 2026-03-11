# CLAUDE.md — AI-Powered Graph Extraction & Curation Platform

Read this file fully before writing any code or making architectural decisions.

---

## 1. Project Purpose

A **session-based, AI-assisted web application** that transforms documents into a curated knowledge graph in Neo4j. Every graph mutation flows through: `Proposal → Human Approval → Deterministic Diff → Execution → Audit`. AI interprets evidence but cannot directly mutate state.

---

## 2. Repository Structure

```
/
├── .github/
│   └── workflows/       # CI pipeline (GitHub Actions)
├── ui/                  # Streamlit frontend (UI layer only — no business logic)
│   ├── components/      # Reusable UI widgets (phase indicator, etc.)
│   └── pages/           # Per-phase page modules
├── api/                 # FastAPI backend (all business logic)
│   ├── common/          # Shared utilities (ARQ pool singleton)
│   ├── routers/         # HTTP route handlers
│   ├── services/        # Domain services (ingestion, extraction, curation)
│   ├── agents/          # Agent components (typed, constrained)
│   ├── graph/           # Neo4j interaction (sole write authority) + safety guards
│   ├── vector/          # Qdrant retrieval (evidence-only, no writes)
│   ├── storage/         # Artifact storage via boto3
│   ├── worker/          # ARQ worker entry point + agent pipeline jobs
│   ├── schema/          # Domain schema definitions
│   ├── proposals/       # Proposal Packet models + S3 storage helpers
│   ├── diff/            # Deterministic diff builder (non-LLM)
│   ├── audit/           # Immutable audit log writers
│   ├── cache/           # Redis-backed cache abstraction layer
│   └── observability/   # Centralized logging, metrics, correlation IDs
├── docs/                # Governance artifacts (specs, skills, ADRs)
│   ├── specs/           # Increment specification documents (SPEC-01 through SPEC-08)
│   ├── skills/          # Cross-cutting skill definitions (SKILL-A, B, C, D)
│   └── adr/             # Architecture Decision Records
├── prompts/             # Versioned prompt templates by job_id
├── fixtures/            # Test fixtures for deterministic components
├── tests/unit/          # Deterministic tests (no network)
├── tests/integration/   # Neo4j Aura integration tests
├── infra/               # IaC files (read context only)
├── docker-compose.yml
├── pyproject.toml       # PEP 621 project definition
└── CLAUDE.md
```

---

## 3. Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | Streamlit |
| Backend | Python 3.12 / FastAPI |
| Worker | ARQ + Redis |
| Graph DB | Neo4j Aura |
| Vector Store | Qdrant |
| Object Storage | boto3 → RustFS (local) / S3 (prod) |
| LLM Gateway | OpenRouter API |
| Package Manager | uv + pyproject.toml |
| Caching | Redis (shared with ARQ) |
| Observability | structlog + in-memory metrics |

---

## 4. Architecture Rules (Non-Negotiable)

### 4.1 Layer Boundaries
- **Streamlit**: UI only — no business logic, graph queries, or agent calls
- **FastAPI**: All domain logic, orchestration, graph writes, validation
- **Neo4j**: Source of truth. Qdrant is evidence-only, never authoritative

### 4.2 Write Authority
- **No autonomous mutation**: All writes flow through `Proposal → Approval → Diff → Execution`
- **No free-form Cypher**: LLMs never generate or execute graph queries
- **No bypasses**: No helpers or convenience functions that skip the approval pipeline

### 4.3 Determinism Requirements
Same inputs must always produce same outputs (no randomness, no LLM):
- All IDs: doc, chunk, node/rel dedupe keys, candidate, proposal, diff
- Candidate generation, diff generation, schema validation, audit records

### 4.4 Fail-Closed Behavior
System must reject (not auto-correct) on:
- Schema version mismatch
- Missing provenance fields
- Missing/invalid approval token before mutation
- Invalid AI output (block, log, emit fallback)
- Missing required env vars at startup

### 4.5 Sandboxed Intelligence
AI agents compose proposals but never: execute mutations, generate Cypher, access retrieval beyond budget, or operate outside assigned candidates.

---

## 5. Identity Conventions

| Artifact | Derivation |
|----------|------------|
| `doc_id` | `hash(run_id + source_identity + content_hash)` |
| `chunk_id` | `hash(doc_id + start_page_locator)` |
| `node_dedupe_key` | `(NodeType, primary_property, schema_version)` |
| `rel_dedupe_key` | `(RelType, start_key, end_key, schema_version)` |
| `proposal_id` | `(run_id, candidate_id, proposal_class)` |
| `diff_id` | hash of diff content |
| `user_hash` | `SHA-256(neo4j_uri + NUL + neo4j_user)` — session key scope |

**Never use `uuid4()` for governed artifact IDs.**

All artifacts must carry: `run_id` and `schema_version`.

---

## 6. Proposal Packet

A Proposal Packet (from Agent-P) contains:
- **Linkage**: `run_id`, `schema_version`, `candidate_id`
- **Intent**: `proposal_class` (canonicalize | normalize | rename | merge | delete | defer)
- **Evidence**: source chunk/document IDs
- **Governance**: `rule_ids[]`, `rationale`, `confidence_score`
- **Targets**: graph element references for diffing

**Never contains**: Cypher, executable instructions, or runnable mutations.

---

## 7. Agent Pipeline

| Agent | Type | Allowed Operations |
|-------|------|-------------------|
| Orchestrator | Non-LLM | Route candidates, assign risk/budget |
| Agent-A | LLM | Read graph/chunks, classify evidence |
| Structural Rec | Non-LLM | Pre-digest collision_context → action + confidence (`api/agents/structural.py`) |
| Agent-B | LLM | Budgeted retrieval (loop-guarded) — **dormant** until curation-panel ingestion |
| Agent-P | LLM | Compose Proposal Packet (anchored on structural recommendation) |
| Diff Builder | Non-LLM | Translate proposal → deterministic diff |
| Approval Gate | Human | Approve / Reject / Defer |
| Agent-C | Tools-only | Apply approved diff (no reasoning) |

**Current flow**: Orchestrator → Agent-A → Structural Rec → Agent-P → Diff Builder → Approval Gate → Agent-C.
Agent-B is skipped (registered but not enqueued). It will activate when curation-panel document ingestion is implemented, allowing users to add new evidence documents that Agent-B can search.

Agent-C requires: `run_id`, `schema_version`, valid `approval_id`, post-apply invariant checks.

---

## 8. UI State Machine

### StateManager (Mandatory)
Direct `st.session_state` access is **banned** outside `StateManager`.

```python
# CORRECT
state = StateManager.get()
state.advance_phase(Phase.EXTRACTION)

# BANNED
st.session_state["current_phase"] = "extraction"
```

### Phases (Strictly Ordered)
| Phase | Name |
|-------|------|
| 0 | Session Initialization |
| 1 | Domain Schema Definition |
| 2 | Document Ingestion & Chunking |
| 3 | AI-Assisted Extraction |
| 4 | Curation |

---

## 9. Development Environment

### Local Setup
```bash
docker compose up
```
Runs: FastAPI, Streamlit, Redis, RustFS, Qdrant

### Docker Rebuild Policy

**Hotpatch first, rebuild only when necessary.**

- **Code-only changes** (Python files in `api/`, `ui/`, `prompts/`): use `./hotpatch.sh` to copy files into running containers and restart. No image rebuild needed.
- **Dependency changes** (`pyproject.toml`): full rebuild required — `docker compose up -d --build`.
- **Dockerfile changes**: full rebuild required.
- **Config/infra changes** (`docker-compose.yml`, `.env`): restart with `docker compose up -d` (no rebuild unless Dockerfile changed).

```bash
# Hotpatch (seconds — code changes only)
./hotpatch.sh

# Full rebuild (only when dependencies or Dockerfiles change)
docker compose up -d --build api ui
```

Dockerfiles use a two-stage `COPY` pattern: dependencies install in a cached layer, source code is copied last. This means even full rebuilds skip package installation when only code changed. All images pre-install CPU-only PyTorch (`--index-url https://download.pytorch.org/whl/cpu`) to avoid pulling CUDA libraries (~3 GB vs ~13 GB).

### Neo4j — Aura Only (No Local Container)
Requires provisioned Aura instance. App refuses to start without credentials.

### Environment Variables
```bash
# Neo4j (dev)
NEO4J_DEV_URI=neo4j+s://xxx.databases.neo4j.io
NEO4J_DEV_USER=neo4j
NEO4J_DEV_PASSWORD=...

# Neo4j (CI)
NEO4J_CI_URI / NEO4J_CI_USER / NEO4J_CI_PASSWORD

# Services
OPENROUTER_API_KEY
S3_ENDPOINT_URL          # http://localhost:9000 for RustFS
S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / S3_BUCKET_NAME
REDIS_URL                # redis://localhost:6379
QDRANT_URL               # Optional: for remote instance

# Observability
LOG_FORMAT                 # json (production) or console (development)
LOG_LEVEL                  # DEBUG, INFO, WARNING, ERROR — default INFO

# Ingestion parser toggles (SPEC-03)
ENABLE_DOCLING             # true/false — enable Docling tier (default true)
ENABLE_UNSTRUCTURED        # true/false — enable Unstructured tier (default true)
ENABLE_RAW_FALLBACK        # true/false — enable raw-text fallback tier (default true)
EMBEDDING_MODEL            # sentence-transformers model name (default all-MiniLM-L6-v2)

# Dry-run mode (SPEC-08)
DRY_RUN                    # true/false — skip graph mutations, log diffs to S3 (default false)
```

---

## 10. Testing Strategy

### Unit Tests (`tests/unit/`)
- Fixture inputs from `/fixtures/`
- Assert idempotency: same input → same output
- No network, no LLM, no Neo4j
- Cover: ID derivation, chunking, candidate detection, diff generation

### Agent Tests
- Test policy and gateway behavior, not model quality
- Stub HTTP calls; assert fallbacks and audit logs on failure

### Integration Tests (`tests/integration/`)
- Target Aura via `NEO4J_CI_*` env vars (skipped if absent)
- Cover: real writes, constraints, post-apply invariants

### Constraints
- Never `uuid4()` for artifact IDs
- Never write to `st.session_state` directly
- Never test LLM output quality

---

## 11. Code Style

- **Package manager**: `uv` (not `pip install`)
- **Project**: `pyproject.toml` (PEP 621)
- **Validation**: Pydantic models define all contracts; invalid payloads rejected, not coerced
- **Linting**: ruff, mypy — must pass CI

---

## 12. Object Storage & Worker

**Storage**: All artifacts via boto3 → RustFS (local) / S3 (prod). Nothing to disk or Neo4j/Qdrant.

**Worker**: Agent pipeline jobs run in ARQ worker (not FastAPI request cycle). FastAPI enqueues to Redis; worker executes. No subprocess spawning.

---

## 13. Infrastructure

Claude reads `/infra/` for context but **never auto-applies infrastructure changes**.
- Cloud: AWS (ECS Fargate)
- CI/CD: GitHub Actions → build → test → deploy

---

## 14. Prompts

- Stored in `/prompts/`, keyed by `job_id` + `template_version`
- No inline prompt strings in agent code
- Templates immutable once used in a run

---

## 15. Claude Directives

### Always Do
- Use `./hotpatch.sh` for code-only changes — never rebuild Docker images unless dependencies or Dockerfiles changed
- Read this file before writing code
- Derive IDs deterministically
- Route all writes through approval pipeline
- Use `StateManager` for session state
- Reference versioned prompt templates
- Fail closed on invalid inputs
- Test fallback behavior, not model quality
- Before implementing any increment, read the relevant spec in [`docs/specs/`](docs/specs/) and all skills in [`docs/skills/`](docs/skills/) — see [§17 Specifications & Skills](#17-specifications--skills) for the full table
- After completing any increment, run the [SKILL-B](docs/skills/SKILL-B-governance.md) governance checklist and update README.md and CLAUDE.md
- Use the centralized logger from `api/observability/logger.py` — never create ad-hoc loggers
- Check the cache before expensive reads — use `api/cache/client.py` with deterministic keys
- Log structured events with correlation IDs — never free-text log messages

### Never Do
- Rebuild Docker images for code-only changes — use `./hotpatch.sh` instead
- Write Cypher in LLM agents
- Allow non-Agent-C graph mutations
- Access `st.session_state` directly
- Use `uuid4()` for governed IDs
- Silently coerce invalid payloads
- Bypass approval gate
- Auto-apply infra changes
- Write artifacts to disk or Neo4j/Qdrant
- Create ad-hoc loggers or use Python's `logging.basicConfig()` directly
- Construct cache keys by string concatenation — use `CacheKey` builders
- Log credentials, API keys, or raw document content
- Perform expensive reads without checking the cache first
- Add business logic to the monitoring UI page

---

## 16. Increment Status

| Increment | Version | Status |
|-----------|---------|--------|
| [SPEC-01](docs/specs/SPEC-01-scaffolding.md) | 0.1.0 | ✅ Complete |
| [SPEC-02](docs/specs/SPEC-02-schema.md) | 0.2.0 | ✅ Complete |
| [SPEC-03](docs/specs/SPEC-03-ingestion.md) | 0.3.0 | ✅ Complete |
| [SPEC-04](docs/specs/SPEC-04-extraction.md) | 0.4.0 | ✅ Complete |
| [SPEC-05](docs/specs/SPEC-05-candidates.md) | 0.5.0 | ✅ Complete |
| [SPEC-06](docs/specs/SPEC-06-manual-curation.md) | 0.6.0 | ✅ Complete |
| [SPEC-07](docs/specs/SPEC-07-agent-pipeline.md) | 0.7.0 | ✅ Complete |
| [SPEC-08](docs/specs/SPEC-08-hardening.md) | 0.8.0 | ✅ Complete |

Do not begin increment N+1 until increment N passes all acceptance criteria and the SKILL-B governance checklist.

---

## 17. Specifications & Skills

### 17.1 How to Use

Before beginning implementation work on any increment:
1. Read this file (CLAUDE.md) in full
2. Identify the current increment from the spec list below
3. Read the relevant `docs/specs/SPEC-*.md` file
4. Read ALL skill files in `docs/skills/`
5. Follow the spec's file generation sequence and acceptance criteria
6. Run the SKILL-B governance checklist after completion

### 17.2 Increment Specifications

Increments are strictly ordered — do not start N+1 until N passes.

| Spec | Inc | Description |
|------|-----|-------------|
| [SPEC-01](docs/specs/SPEC-01-scaffolding.md) | 1 | Scaffolding, session lifecycle, logging, caching, monitoring |
| [SPEC-02](docs/specs/SPEC-02-schema.md) | 2 | Domain schema definition (Phase 1) |
| [SPEC-03](docs/specs/SPEC-03-ingestion.md) | 3 | Document ingestion & chunking (Phase 2) |
| [SPEC-04](docs/specs/SPEC-04-extraction.md) | 4 | AI-assisted extraction, worker monitoring (Phase 3) |
| [SPEC-05](docs/specs/SPEC-05-candidates.md) | 5 | Deterministic candidate generation (Curation Layer 1) |
| [SPEC-06](docs/specs/SPEC-06-manual-curation.md) | 6 | Manual curation, evidence retrieval, proposal pipeline (Layer 2) |
| [SPEC-07](docs/specs/SPEC-07-agent-pipeline.md) | 7 | AI curation agent pipeline, agent telemetry (Layer 3) |
| [SPEC-08](docs/specs/SPEC-08-hardening.md) | 8 | Monitoring polish, logging hardening, CI, documentation |

### 17.3 Cross-Cutting Skills

Read and follow ALL skills during every implementation task.

| Skill | Scope | Description |
|-------|-------|-------------|
| [SKILL-A](docs/skills/SKILL-A-api-contracts.md) | Every endpoint | Pydantic request/response models, validation |
| [SKILL-B](docs/skills/SKILL-B-governance.md) | Every change | Folder structure, layers, docs, semver, refactoring |
| [SKILL-C](docs/skills/SKILL-C-packaging.md) | All increments | Entry points, imports, init files, dependencies |
| [SKILL-D](docs/skills/SKILL-D-observability.md) | Every module | Logging, caching, monitoring |

---

## 18. Skill Rules (Condensed)

The rules below are extracted from the skill files. Refer to the full skill files in `docs/skills/` only when running verification checklists.

### SKILL-A: API Contracts

- **R-A1**: Every FastAPI endpoint must have a Pydantic request model and a response model defined in `models.py` within the relevant `api/` subdirectory
- **R-A2**: All responses extend `BaseResponse(run_id, status, errors[])`
- **R-A3**: Inter-service calls pass typed Pydantic objects — raw dicts banned at module boundaries
- **R-A4**: Invalid payloads rejected at router layer with structured `ErrorDetail` — never silently coerced
- **R-A5**: Request/response models must exist in the same commit as their route handler
- **R-A6**: Auto-generated OpenAPI schema must stay accurate — fix models, not schema overrides

### SKILL-B: Governance

- **R-B1**: `CLAUDE.md` is the constitution — if a change conflicts, the change is wrong
- **R-B2**: Every file must land in the correct directory per §2 (see folder table in `SKILL-B`)
- **R-B3**: Layer violations are blockers — `ui/` must not import from `api/graph/`, `api/agents/`, `api/vector/`, `api/diff/`, or `api/audit/`; no `st.session_state` outside `ui/state.py`
- **R-B4**: New modules/dirs → update §2; new env vars → update §9; new agents → update §7; architectural decisions → new ADR
- **R-B5**: `README.md` updated every increment (status, setup, endpoints, limitations)
- **R-B6**: Semver in `pyproject.toml` — PATCH for bugfixes, MINOR (0.X.0) per completed increment, MAJOR for breaking changes
- **R-B7**: Proactive refactoring: >400 lines → split; >5 params → Pydantic model; duplicated in 2+ modules → extract; model used in 3+ → move to `api/models/`
- **R-B8**: New spec/skill docs → update §17

### SKILL-C: Packaging

- **R-C1**: `pyproject.toml` defines entry points: `api-server = "api.main:run"`, `arq-worker = "api.worker.entry:run"`
- **R-C2**: Absolute imports only (`from api.x import y`) — relative imports banned
- **R-C3**: Every Python directory must have `__init__.py`
- **R-C4**: All dependencies in `pyproject.toml` with version bounds (`>=min,<max`)
- **R-C5**: Per-service Dockerfiles: `Dockerfile.api`, `Dockerfile.worker`, `Dockerfile.ui`
- **R-C6**: No hardcoded file paths — derive from env vars or config

### SKILL-D: Observability

- **R-D1**: All logging via `api/observability/logger.py` factory → `get_logger(__name__)`. No `logging.basicConfig()`
- **R-D2**: Correlation IDs on every request (from `run_id` or generated), propagated through logs, ARQ jobs, downstream calls
- **R-D3**: Structured log events only: `logger.info("event_name", key=val)` — no f-string messages
- **R-D4**: Log levels: DEBUG (diagnostics), INFO (operations), WARNING (degraded), ERROR (failures), CRITICAL (system)
- **R-D5**: Never log credentials, API keys, raw document content, or full LLM prompts in production
- **R-D6**: Redis-backed cache via `api/cache/client.py` — no direct Redis calls outside this module
- **R-D7**: Cache keys via `CacheKey` builders in `api/cache/keys.py` — no ad-hoc string concatenation
- **R-D8**: Cache: locked schema (run lifetime), manifest (1h), graph queries (5min), chunks (30min), candidates (5min)
- **R-D9**: Cache misses are normal (DEBUG level) — system falls through to source; Redis failures logged at WARNING, never fail-closed
- **R-D10**: After any graph mutation, invalidate all run-scoped cache keys (post-execution hook in Agent-C)
- **R-D11**: Monitoring endpoints introduced progressively per spec (see `SKILL-D` for the full table)
- **R-D12**: Frontend monitoring page is read-only, no business logic — renders data from backend endpoints
- **R-D13**: Monitoring never blocks pipeline — in-memory metrics, ring buffer for logs, Redis INFO for cache stats
