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
| SPEC-06   | 0.6.0   | Manual curation & proposal pipeline | Pending |
| SPEC-07   | 0.7.0   | AI curation agent pipeline | Pending |
| SPEC-08   | 0.8.0   | Monitoring polish, CI, documentation | Pending |

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

### Available Endpoints (Increments 1–5)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Basic backend health check |
| GET | `/api/monitoring/health` | Per-service connectivity with latency |
| GET | `/api/monitoring/logs/recent` | Recent log entries (filterable by level) |
| GET | `/api/monitoring/run/{run_id}` | Run-level summary |
| GET | `/api/monitoring/workers` | ARQ queue depth and active worker count |
| GET | `/api/monitoring/jobs/{run_id}` | Per-chunk extraction job statuses |
| POST | `/api/schema/propose` | Generate candidate schema via LLM for human review |
| POST | `/api/schema/approve` | Lock the domain schema for a run (immutable after this call) |
| GET | `/api/schema/{run_id}` | Retrieve locked schema (cache-first, no Neo4j query) |
| POST | `/api/documents/ingest` | Parse, chunk, and index an uploaded PDF or DOCX document |
| GET | `/api/documents/{run_id}` | List all successfully ingested documents for a run |
| GET | `/api/documents/{run_id}/{doc_id}/chunks` | Return chunk metadata with quality flag highlights |
| POST | `/api/extraction/run` | Enqueue extraction jobs for all chunks in a run |
| GET | `/api/extraction/status/{run_id}` | Aggregated extraction progress for a run |
| GET | `/api/extraction/results/{run_id}` | Extracted nodes and edges written to Neo4j |
| POST | `/api/curation/candidates/generate` | Run all five deterministic detectors; cache results with 5-min TTL |
| GET | `/api/curation/candidates/{run_id}` | Return cached candidates grouped by type and ordered by severity |

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

## Known Limitations (Increment 5)

- No manual curation or proposal pipeline yet (SPEC-06 through SPEC-08)
- `GET /api/monitoring/run/{run_id}` returns `found=false` until a server-side run registry is added in a later increment
- Seven modules exceed the ~400-line governance limit and are scheduled for refactoring (SKILL-B R-B7): `api/services/ingestion.py` (677), `ui/pages/ingestion.py` (687), `api/services/curation/candidates.py` (616), `api/vector/indexer.py` (534), `api/routers/curation.py` (546), `api/storage/artifacts.py` (470), `api/routers/documents.py` (462)
- ARQ `_get_arq_pool()` is duplicated in `api/routers/extraction.py` and `api/routers/monitoring.py`; consolidation into `api/common/arq_pool.py` is a SKILL-B R-B7 item
- Per-page locators from Docling/Unstructured are approximated; exact page-level attribution is not yet tracked per chunk
- Qdrant collection (`chunks_{run_id}`) is not cleaned up on run deletion — no lifecycle management until SPEC-08

---

## Project Structure

```
/
├── ui/                  # Streamlit frontend (UI layer only)
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
├── prompts/             # Versioned prompt templates
├── fixtures/            # Test fixtures
├── tests/               # Unit and integration tests
└── infra/               # IaC files (read-only reference)
```
