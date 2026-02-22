# neo4all — AI-Powered Graph Extraction & Curation Platform

A session-based, AI-assisted web application that transforms documents into a curated knowledge graph in Neo4j. Every graph mutation flows through a governed pipeline: `Proposal → Human Approval → Deterministic Diff → Execution → Audit`.

---

## Current Status

| Increment | Version | Description | Status |
|-----------|---------|-------------|--------|
| SPEC-01   | 0.1.0   | Scaffolding, session lifecycle, logging, caching, monitoring | ✅ Complete  |
| SPEC-02   | 0.2.0   | Domain schema definition | ✅ Complete |
| SPEC-03   | 0.3.0   | Document ingestion & chunking | Pending |
| SPEC-04   | 0.4.0   | AI-assisted extraction | Pending |
| SPEC-05   | 0.5.0   | Deterministic candidate generation | Pending |
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
arq-worker        # starts ARQ worker
```

---

## API

FastAPI auto-generates interactive docs at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### Available Endpoints (Increments 1–2)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Basic backend health check |
| GET | `/api/monitoring/health` | Per-service connectivity with latency |
| GET | `/api/monitoring/logs/recent` | Recent log entries (filterable) |
| GET | `/api/monitoring/run/{run_id}` | Run-level summary |
| POST | `/api/schema/propose` | Generate candidate schema via LLM for human review |
| POST | `/api/schema/approve` | Lock the domain schema for a run (immutable after this call) |
| GET | `/api/schema/{run_id}` | Retrieve locked schema (cache-first, no Neo4j query) |

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

## Known Limitations (Increment 2)

- No document ingestion, extraction, or curation yet (SPEC-03 through SPEC-08)
- Schema definition phase (Phase 1) is complete — ingestion (Phase 2) is next
- Monitoring endpoints return stub data until downstream services are implemented
- ARQ worker entry point defined but worker jobs not yet implemented
- `ui/pages/schema.py` exceeds the ~400-line module limit (716 lines); scheduled for split into `schema.py` + `schema_helpers.py` before SPEC-03

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
