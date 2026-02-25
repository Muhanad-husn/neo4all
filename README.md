# neo4all — AI-Powered Graph Extraction & Curation Platform

A session-based, AI-assisted web application that transforms documents into a curated knowledge graph in Neo4j. Every graph mutation flows through a governed pipeline: `Proposal → Human Approval → Deterministic Diff → Execution → Audit`.

---

## Current Status

| Increment | Version | Description | Status |
|-----------|---------|-------------|--------|
| SPEC-01   | 0.1.0   | Scaffolding, session lifecycle, logging, caching, monitoring | ✅ Complete  |
| SPEC-02   | 0.2.0   | Domain schema definition | ✅ Complete |
| SPEC-03   | 0.3.0   | Document ingestion & chunking | ✅ Complete |
| SPEC-04   | 0.4.0   | AI-assisted extraction & ARQ worker | ✅ Complete |
| SPEC-05   | 0.5.0   | Deterministic candidate generation | ✅ Complete |
| SPEC-06   | 0.6.0   | Manual curation & proposal pipeline | ✅ Complete |
| SPEC-07   | 0.7.0   | AI curation agent pipeline | ✅ Complete |
| SPEC-08   | 0.8.0   | Monitoring polish, CI, documentation | ✅ Complete |

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | Streamlit |
| Backend | Python 3.12 / FastAPI |
| Worker | ARQ + Redis |
| Graph DB | Neo4j Aura (cloud-only, no local container) |
| Vector Store | Qdrant |
| Object Storage | boto3 → RustFS (local) / S3 (prod) |
| LLM Gateway | OpenRouter API |
| Package Manager | uv |
| Caching | Redis (shared with ARQ) |
| Observability | structlog + in-memory metrics |

---

## Prerequisites

- Docker & Docker Compose
- A provisioned [Neo4j Aura](https://console.neo4j.io) instance (the app refuses to start without credentials)
- [uv](https://docs.astral.sh/uv/) for local development

---

## Setup

### 1. Configure environment variables

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

Required variables:

```bash
# Neo4j Aura (dev)
NEO4J_DEV_URI=neo4j+s://xxxx.databases.neo4j.io
NEO4J_DEV_USER=neo4j
NEO4J_DEV_PASSWORD=<your-password>

# OpenRouter
OPENROUTER_API_KEY=sk-or-...

# Object storage (RustFS in local, S3 in prod)
S3_ENDPOINT_URL=http://localhost:9000
S3_ACCESS_KEY_ID=<key>
S3_SECRET_ACCESS_KEY=<secret>
S3_BUCKET_NAME=neo4all

# Redis (set automatically by docker-compose)
REDIS_URL=redis://localhost:6379

# Observability
LOG_FORMAT=console   # json (production) | console (development)
LOG_LEVEL=INFO
```

### 2. Start local services

```bash
docker compose up
```

This starts: FastAPI (`:8000`), Streamlit (`:8501`), Redis (`:6379`), RustFS (`:9000`), Qdrant (`:6333`).

**Note**: Neo4j runs on Aura — no local Neo4j container.

### 3. Local development (without Docker)

```bash
uv pip install -e ".[dev]"
api-server        # starts FastAPI on :8000
streamlit run ui/app.py   # starts Streamlit on :8501
arq-worker        # starts ARQ worker (api.worker.entry:WorkerSettings)
```

> **Note (SPEC-04):** The ARQ worker must be running for Phase 3 extraction jobs to execute. The worker requires the same environment variables as the API server, plus a locked domain schema (Phase 1 complete) before extraction can be triggered.

---

## API

FastAPI auto-generates interactive docs at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Endpoints

#### Health & Monitoring

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Basic backend health check |
| GET | `/api/monitoring/health` | Per-service connectivity with latency |
| GET | `/api/monitoring/logs/recent` | Recent log entries (filterable by level) |
| GET | `/api/monitoring/run/{run_id}` | Run-level summary |
| GET | `/api/monitoring/workers` | ARQ queue depth and active worker count |
| GET | `/api/monitoring/jobs/{run_id}` | Per-chunk extraction job statuses |
| GET | `/api/monitoring/metrics` | Aggregated LLM usage per agent type (tokens, cost, invocations) |
| GET | `/api/monitoring/agents/{run_id}` | Per-candidate agent chain telemetry for a run |
| GET | `/api/monitoring/cache` | Cache hit/miss ratio, key count, memory usage |

#### Schema (Phase 1)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/schema/propose` | Generate candidate schema via LLM for human review |
| POST | `/api/schema/approve` | Lock the domain schema for a run (immutable after this call) |
| GET | `/api/schema/{run_id}` | Retrieve locked schema (cache-first, no Neo4j query) |

#### Documents (Phase 2)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/documents/ingest` | Parse, chunk, and index an uploaded PDF or DOCX document |
| GET | `/api/documents/{run_id}` | List all successfully ingested documents for a run |
| GET | `/api/documents/{run_id}/{doc_id}/chunks` | Return chunk metadata with quality flag highlights |

#### Extraction (Phase 3)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/extraction/run` | Enqueue extraction jobs for all chunks in a run |
| GET | `/api/extraction/status/{run_id}` | Aggregated extraction progress for a run |
| GET | `/api/extraction/results/{run_id}` | Extracted nodes and edges written to Neo4j |

#### Curation (Phase 4)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/curation/candidates/generate` | Run all five deterministic detectors; cache results with 5-min TTL |
| GET | `/api/curation/candidates/{run_id}` | Return cached candidates grouped by type and ordered by severity |
| POST | `/api/curation/propose` | Submit a manual Proposal Packet for a candidate |
| GET | `/api/curation/proposals/{run_id}` | List all proposals for a run (with state and diff summary) |
| POST | `/api/curation/proposals/{id}/execute` | Build diff and execute an approved proposal via Agent-C |
| GET | `/api/curation/evidence/{candidate_id}` | Retrieve Qdrant evidence chunks for a candidate |
| POST | `/api/curation/evidence/query` | Ad-hoc evidence query by dedupe_key, doc, or semantic similarity |
| POST | `/api/curation/proposals/{id}/approve` | Issue approval_id (single-step for low-risk; phase 1 for merge/delete) |
| POST | `/api/curation/proposals/{id}/confirm` | Phase 2 confirmation for high-risk proposals (merge, delete) |
| POST | `/api/curation/proposals/{id}/reject` | Reject a proposal (terminal state) |
| POST | `/api/curation/proposals/{id}/defer` | Defer a proposal for later review |

#### Graph Explorer

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/graph/nodes/{run_id}/count` | Total node count by type for a run |
| GET | `/api/graph/nodes/{run_id}` | Paginated node browser (max 50/page, filterable by node_type) |
| GET | `/api/graph/edges/{run_id}/count` | Total edge count by type for a run |
| GET | `/api/graph/edges/{run_id}` | Paginated edge browser (max 50/page, filterable by edge_type) |

---

## Phase 1: Domain Schema Definition

Before any documents can be ingested, a domain schema must be defined and locked for the run. The workflow is:

1. **Describe the domain** — enter a plain-language description of the domain (e.g. "scientific papers on climate change").
2. **AI proposes a schema** — `POST /api/schema/propose` sends the description to the LLM (via OpenRouter) and returns typed candidate node and edge types for review.
3. **Review and edit** — the UI presents the proposal in editable node and edge type tables. Any type, primary property, or qualifier can be adjusted before approval.
4. **Approve to lock** — `POST /api/schema/approve` freezes the schema for the run. A deterministic `version_hash` is computed from the canonical sorted representation. The locked schema is cached in Redis indefinitely (no TTL) and cannot be modified for the lifetime of the run.

### Prompt Template System

LLM prompts are stored as versioned YAML templates under `prompts/`, keyed by `job_id` and `template_version`:

```
prompts/
└── schema_propose/
    └── v1.yaml     # job_id=schema_propose, template_version=v1
```

No prompt strings are inlined in service code. Templates are immutable once used in a run. Each template carries a `system` prompt and a `user` template with named placeholders (e.g. `{domain_description}`).

---

## Phase 2: Document Ingestion & Chunking

Once a schema is locked, source documents can be uploaded and indexed. The workflow is:

1. **Upload a document** — select a PDF or DOCX file in the Phase 2 UI. Multiple documents can be uploaded one at a time.
2. **Three-tier parser fallback** — the backend attempts each enabled parser in order and uses the first that succeeds:
   - **Docling** (primary) — full structural parsing: titles, paragraphs, tables, images, captions. Exports Markdown for high-fidelity chunking.
   - **Unstructured** (secondary) — broad format support; activated automatically when Docling raises an exception or returns empty output.
   - **Raw text** (tertiary) — PyPDF2 (PDF) or python-docx (DOCX) flat extraction. No structural metadata. All chunks from this tier carry the `raw_fallback` quality flag.
   - If all enabled tiers fail → hard reject with a structured error response.
3. **Semantic chunking** — extracted text is segmented respecting headings (chunk boundaries), table isolation (always standalone), and configurable character/token limits. `chunk_id = SHA-256(doc_id + start_page + chunk_index)` — fully deterministic.
4. **Quality flags** — soft signals attached per chunk at ingestion time (processing continues):
   - `raw_fallback` — tertiary raw-text parser was used; no structural metadata present
   - `low_ocr_confidence` — OCR confidence score below threshold (0.7)
   - `low_text_density` — fewer than 20 characters in the chunk
5. **Qdrant indexing** — chunks are embedded via sentence-transformers and upserted to a run-scoped collection (`chunks_{run_id}`) for evidence retrieval in Phase 3. Qdrant is evidence-only and never authoritative.
6. **Manifest persistence** — a `DocumentManifest` (doc_id, chunk_ids, content_hash, parser_config_hash) is stored in S3 and cached in Redis. On re-upload of an unchanged file with the same parser configuration, the parse step is skipped entirely (incremental reruns).

Parser tiers are individually toggleable via `ENABLE_DOCLING`, `ENABLE_UNSTRUCTURED`, `ENABLE_RAW_FALLBACK` environment variables — useful in CI environments where parser libraries may not be installed.

---

## Phase 3: AI-Assisted Extraction

Once documents are ingested, extraction enqueues one ARQ job per chunk. Each job sends the chunk text and locked schema to the LLM (via OpenRouter), validates the response, and writes MERGE operations to Neo4j.

**Prerequisites**: Domain schema must be locked (Phase 1 complete). The ARQ worker must be running.

1. **Trigger extraction** — `POST /api/extraction/run` enqueues one `extraction_job` per chunk in the run. Returns immediately; jobs execute in the background ARQ worker.
2. **LLM extraction** — Each job loads the chunk text from S3, retrieves the locked schema from the Redis cache (`CacheKey.schema(run_id)`), and sends both to OpenRouter using `prompts/extraction/v1.yaml`. Output is validated against `ExtractionResult` (fail-closed: malformed LLM output is blocked and logged, never silently accepted).
3. **Graph writes** — Valid extracted nodes and edges are written to Neo4j via `MERGE` on `node_dedupe_key` / `rel_dedupe_key`. Rerunning the same chunk always produces the same graph state (idempotent).
4. **Per-job status tracking** — Each job writes a `ChunkJobStatus` record to Redis (`job:{run_id}:{chunk_id}`). `GET /api/monitoring/jobs/{run_id}` scans these keys for real-time progress. Status values: `queued → running → complete | failed`.
5. **Worker monitoring** — `GET /api/monitoring/workers` returns queue depth, active job count, and live worker count directly from Redis (no database queries).

### Prompt Template: Extraction

```
prompts/
├── schema_propose/
│   └── v1.yaml     # job_id=schema_propose, template_version=v1
└── extraction/
    └── v1.yaml     # job_id=extraction, template_version=v1
```

---

## Phase 4: Curation — Layer 1 (Candidate Generation)

Once extraction is complete, deterministic pre-curation detects data quality issues in the graph before any AI or human curation decisions are made.

1. **Trigger generation** — `POST /api/curation/candidates/generate` runs all five detectors against the Neo4j graph for a run. Results are cached with a 5-minute TTL.
2. **Five detectors (zero-LLM)**:
   - **Exact node duplicate** — same `NodeType` + `dedupe_key` appears under different IDs.
   - **Exact relationship duplicate** — same rel type + same start/end `dedupe_key` appears more than once.
   - **Probable duplicate** — same-type node pairs with Jaro-Winkler similarity ≥ 0.90 + shared context score (pure Python, no external library).
   - **Canonical/inverse violation** — edge direction or inverse-mapping rules defined in the locked schema are violated.
   - **Structural anomaly** — orphan nodes, degree outliers (3σ), missing provenance fields, or qualifier gaps.
3. **View candidates** — `GET /api/curation/candidates/{run_id}` returns candidates grouped by type and ordered by severity (critical → high → medium → low).
4. **Deterministic IDs** — `candidate_id = SHA-256(run_id + schema_version + candidate_type + sorted(involved_element_refs) + detection_method)`. Same graph situation always produces the same `candidate_id`, enabling idempotent re-runs.

---

## Phase 4: Curation — Layer 2 (Manual Curation & Proposal Pipeline)

Once candidates are generated, each can be actioned through the governed mutation pipeline.

1. **Review evidence** — `GET /api/curation/evidence/{candidate_id}` retrieves ranked Qdrant evidence chunks (text, source doc, page locator, relevance score) for a candidate. `POST /api/curation/evidence/query` supports ad-hoc lookup by dedupe_key, doc, or semantic similarity.
2. **Submit a proposal** — `POST /api/curation/propose` creates a `ProposalPacket` with intent (canonicalize, normalize, rename, merge, delete, defer), evidence references, governance rule IDs, rationale, and confidence score. `proposal_id` is deterministic: `SHA-256(run_id + candidate_id + proposal_class)`.
3. **Approval gate** — Low-risk proposals (canonicalize, normalize, rename, defer): single-step `POST .../approve` issues an `approval_id`. High-risk proposals (merge, delete): two-phase — phase 1 returns a `confirmation_token`; phase 2 `POST .../confirm` validates the token and issues the `approval_id`. `POST .../reject` or `POST .../defer` terminates the proposal.
4. **Diff build + execution** — `POST /api/curation/proposals/{id}/execute` feeds the approved `ProposalPacket` to the deterministic `DiffBuilder` (no LLM) producing a `DiffPlan` with a stable `diff_id = SHA-256(diff_content)`. Agent-C validates the `approval_id`, applies typed graph mutations, runs post-apply invariant checks, and appends an immutable `AuditRecord` to S3.
5. **Cache invalidation** — After every Agent-C mutation, all `gq:{run_id}:*` (graph query) and `candidates:{run_id}:*` (detection) cache keys are invalidated.
6. **Graph explorer** — `GET /api/graph/nodes/{run_id}` and `GET /api/graph/edges/{run_id}` provide paginated (max 50/page), type-filterable read-only browsing of the current graph state. Available in the **Graph Explorer** Streamlit page.

---

## Phase 4: Curation — Layer 3 (AI Agent Pipeline)

Once candidates are generated and evidence is available, the AI curation agent pipeline can process candidates in batch through a governed multi-agent chain.

1. **Orchestrator (non-LLM)** — assigns a risk class (low/medium/high) and tool budget (token limits, cost cap, retrieval rounds) to each candidate. Deterministic: same candidate + config always produces the same decision. Batch size capped at 50.
2. **Agent-A: Evidence Assembly (LLM)** — retrieves graph context and Qdrant chunks for a candidate, classifies evidence (supporting/corroborating/conflicting) via LLM, computes a sufficiency score. Output: typed `EvidenceReport`. Does not decide actions.
3. **Agent-B: Retrieval Augmentation (LLM)** — triggered only when Agent-A flags insufficient evidence. Performs loop-guarded retrieval rounds (max N per budget) to gather additional evidence. Terminates on: threshold met, max rounds, budget exhausted, or voluntary empty query.
4. **Agent-P: Proposal Composer (LLM)** — receives candidate + evidence report, selects proposal class, cites rule IDs and evidence, writes rationale. Outputs governed `ProposalPacket` — never Cypher, never executable instructions. A regex safety guard rejects any output containing Cypher query syntax or executable code patterns.
5. **Pipeline execution** — all three agent jobs run as chained ARQ worker tasks: `evidence_assembly_job → retrieval_augmentation_job (conditional) → proposal_composition_job`. AI proposals enter the same governed pipeline as manual curation (propose → diff → approval → execution).
6. **Batch processing** — the curation UI supports selecting multiple candidates and dispatching them through the agent pipeline in parallel. Progress is tracked per-candidate in real time.
7. **Agent telemetry** — per-agent metrics (tokens in/out, cost estimate, execution time, evidence score) are recorded per `(run_id, candidate_id, agent_name)` and surfaced via `GET /api/monitoring/metrics` (aggregated) and `GET /api/monitoring/agents/{run_id}` (per-candidate). The monitoring UI displays agent pipeline panels with budget consumption gauges.

### Prompt Templates: Agent Pipeline

```
prompts/
├── evidence_assembly/
│   └── v1.yaml     # job_id=evidence_assembly, template_version=v1
├── retrieval_augmentation/
│   └── v1.yaml     # job_id=retrieval_augmentation, template_version=v1
└── proposal_composer/
    └── v1.yaml     # job_id=proposal_composer, template_version=v1
```

---

## UI Pages

| Page | Path | Description |
|------|------|-------------|
| Dashboard | `ui/pages/dashboard.py` | Run summary, graph statistics, proposal breakdown |
| Schema | `ui/pages/schema.py` | Phase 1: domain description → AI proposal → edit → lock |
| Ingestion | `ui/pages/ingestion.py` | Phase 2: upload documents, view chunks and quality flags |
| Extraction | `ui/pages/extraction.py` | Phase 3: trigger extraction, monitor per-chunk job progress |
| Curation | `ui/pages/curation.py` | Phase 4: candidate review, evidence, proposals, approval pipeline, agent dispatch |
| Graph Explorer | `ui/pages/graph_explorer.py` | Paginated node/edge browser with type filter |
| Monitoring | `ui/pages/monitoring.py` | Health, logs, workers, jobs, cache stats, agent telemetry, alerting |

All pages use `StateManager` for session state (no direct `st.session_state` access) and communicate with the backend exclusively via HTTP.

---

## CI Pipeline

GitHub Actions (`.github/workflows/ci.yml`) runs on push/PR to `main`:

| Job | Description |
|-----|-------------|
| `lint` | `ruff check .` |
| `typecheck` | `mypy api/ ui/` (strict mode) |
| `unit-tests` | `pytest tests/unit/` with Redis service container |
| `integration-tests` | `pytest tests/integration/` — **skipped** unless `NEO4J_CI_*` secrets are configured |
| `docker-build` | `docker compose build` |

---

## Dry-Run Mode

Set `DRY_RUN=true` to run the full pipeline without graph mutations. Agent-C skips all Neo4j writes. Diffs and proposals are still generated, logged to S3, and visible in the UI. Useful for:
- Validating the pipeline end-to-end before committing to graph changes
- Reviewing what mutations *would* happen on a new dataset
- CI environments without a live Neo4j instance

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NEO4J_DEV_URI` | Yes | — | Neo4j Aura connection URI |
| `NEO4J_DEV_USER` | Yes | — | Neo4j username |
| `NEO4J_DEV_PASSWORD` | Yes | — | Neo4j password |
| `NEO4J_CI_URI` | No | — | CI Neo4j URI (integration tests skip if absent) |
| `NEO4J_CI_USER` | No | — | CI Neo4j username |
| `NEO4J_CI_PASSWORD` | No | — | CI Neo4j password |
| `OPENROUTER_API_KEY` | Yes | — | LLM gateway API key |
| `S3_ENDPOINT_URL` | Yes | — | Object storage endpoint (`http://localhost:9000` for RustFS) |
| `S3_ACCESS_KEY_ID` | Yes | — | Object storage access key |
| `S3_SECRET_ACCESS_KEY` | Yes | — | Object storage secret key |
| `S3_BUCKET_NAME` | Yes | — | Object storage bucket name |
| `REDIS_URL` | Yes | — | Redis connection URL |
| `QDRANT_URL` | No | — | Remote Qdrant instance URL |
| `LOG_FORMAT` | No | `json` | `json` (production) or `console` (development) |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `ENABLE_DOCLING` | No | `true` | Enable Docling parser tier |
| `ENABLE_UNSTRUCTURED` | No | `true` | Enable Unstructured parser tier |
| `ENABLE_RAW_FALLBACK` | No | `true` | Enable raw-text fallback parser tier |
| `EMBEDDING_MODEL` | No | `all-MiniLM-L6-v2` | Sentence-transformers model for chunk embeddings |
| `DRY_RUN` | No | `false` | Skip graph mutations; log diffs to S3 |

---

## Development Workflow

```bash
# Install dependencies (local dev)
uv pip install -e ".[dev]"

# Run linting and type checks
ruff check .
mypy api/ ui/

# Run unit tests (no network required)
pytest tests/unit/ -v

# Run integration tests (requires Neo4j Aura + Redis)
pytest tests/integration/ -v

# Start all services via Docker
docker compose up
```

---

## Architecture Decision Records

| ADR | Decision |
|-----|----------|
| [ADR-001](docs/adr/001-parser-fallback-chain.md) | Three-tier parser fallback: Docling → Unstructured → raw text |
| [ADR-002](docs/adr/002-no-local-neo4j.md) | Neo4j Aura only — no local graph database container |
| [ADR-003](docs/adr/003-deterministic-ids.md) | SHA-256 content-derived IDs for all governed artifacts |

---

## Known Limitations (v0.8.0)

- `GET /api/monitoring/run/{run_id}` returns `found=false` until a server-side run registry is added
- Several modules exceed the ~400-line governance limit (SKILL-B R-B7) and are scheduled for refactoring
- ARQ `_get_arq_pool()` is duplicated in `api/routers/extraction.py` and `api/routers/monitoring.py`; consolidation planned
- Per-page locators from Docling/Unstructured are approximated; exact page-level attribution is not yet tracked per chunk
- Edge type filter in graph explorer is a Python-side slice on the full cached list; no typed graph reader method for edge type filtering
- Qdrant collections (`chunks_{run_id}`) are not cleaned up on run deletion

---

## Project Structure

```
/
├── .github/workflows/   # CI pipeline (GitHub Actions)
├── ui/                  # Streamlit frontend (UI layer only)
│   ├── components/      # Reusable UI widgets (phase indicator)
│   └── pages/           # Per-phase page modules
├── api/                 # FastAPI backend (all business logic)
│   ├── routers/         # HTTP route handlers
│   ├── services/        # Domain services
│   ├── agents/          # Agent components
│   ├── graph/           # Neo4j interaction
│   ├── vector/          # Qdrant retrieval
│   ├── storage/         # Artifact storage
│   ├── worker/          # ARQ worker entry point
│   ├── schema/          # Domain schema definitions
│   ├── proposals/       # Proposal Packet models
│   ├── diff/            # Deterministic diff builder
│   ├── audit/           # Immutable audit log writers
│   ├── cache/           # Redis-backed cache abstraction
│   └── observability/   # Logging, metrics, correlation IDs
├── docs/                # Specs, skills, ADRs
│   ├── specs/           # Increment specifications (SPEC-01 through SPEC-08)
│   ├── skills/          # Cross-cutting skills (SKILL-A, B, C, D)
│   └── adr/             # Architecture Decision Records
├── prompts/             # Versioned prompt templates
├── fixtures/            # Test fixtures
├── tests/               # Unit and integration tests
└── infra/               # IaC files (read-only reference)
```
