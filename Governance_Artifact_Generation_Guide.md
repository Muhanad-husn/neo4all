# Instructional Guide: Governance Artifact Generation

**Purpose**: This document instructs Claude Code to generate specification and skill documents as governance artifacts, then update `CLAUDE.md` to reference them. These artifacts are **documentation only** — no implementation code is produced. Once complete, `CLAUDE.md` becomes the single entry point that directs Claude Code to the right spec or skill document during future implementation work.

---

## How This Works

```
CLAUDE.md (root authority)
│
├── References → docs/specs/SPEC-*.md    (what to build, per increment)
├── References → docs/skills/SKILL-*.md  (how to behave, cross-cutting)
│
└── Claude Code reads CLAUDE.md before any work
    → CLAUDE.md points to relevant spec/skill
    → Claude Code reads that spec/skill
    → Claude Code implements accordingly
```

**CLAUDE.md is never bloated with implementation details.** It stays lean and referential. The specs and skills carry the detail.

---

## Step 1: Create the Directory Structure

Claude Code must create the following directories and placeholder files:

```
docs/
├── specs/                    # Increment specification documents
│   ├── SPEC-01-scaffolding.md
│   ├── SPEC-02-schema.md
│   ├── SPEC-03-ingestion.md
│   ├── SPEC-04-extraction.md
│   ├── SPEC-05-candidates.md
│   ├── SPEC-06-manual-curation.md
│   ├── SPEC-07-agent-pipeline.md
│   └── SPEC-08-hardening.md
├── skills/                   # Cross-cutting skill documents
│   ├── SKILL-A-api-contracts.md
│   ├── SKILL-B-governance.md
│   └── SKILL-C-packaging.md
└── adr/                      # Architecture Decision Records (created during implementation)
    └── .gitkeep
```

### Command

```bash
mkdir -p docs/specs docs/skills docs/adr
touch docs/adr/.gitkeep
```

---

## Step 2: Generate Skill Documents

Generate these three files first — they govern how all specs are implemented.

---

### File: `docs/skills/SKILL-A-api-contracts.md`

```markdown
# SKILL-A: Full-Stack API Contract Discipline

**Applies to**: Every increment that introduces or modifies an API endpoint.
**Authority**: This skill is referenced from CLAUDE.md Section 17.

---

## Purpose

Every API boundary in this project is defined by typed Pydantic models. This skill ensures contracts are explicit, validated at boundaries, and committed alongside route handlers.

---

## Rules

### R-A1: Pydantic Models Are Mandatory

Every FastAPI endpoint must have:
- A **request model** (Pydantic `BaseModel` subclass) defining the accepted input
- A **response model** (Pydantic `BaseModel` subclass) defining the returned output

Models are defined in a `models.py` file within the relevant `api/` subdirectory. They are never defined inline in route handlers.

### R-A2: Standard Response Envelope

All response models must include:
```python
class BaseResponse(BaseModel):
    run_id: str
    status: Literal["success", "error", "partial"]
    errors: list[ErrorDetail] = []

class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None
```

Typed payloads extend `BaseResponse` with their specific fields.

### R-A3: Inter-Service Contracts

All calls between `api/` submodules (e.g., `api/services/` calling `api/graph/`) must pass typed Pydantic objects. Raw dicts are banned at module boundaries.

### R-A4: Validation at the Boundary

Invalid payloads are rejected at the router layer with a structured error response. The system never silently coerces, patches, or passes through invalid input.

### R-A5: Co-Located Commits

When a new endpoint is added, its request/response models must exist in the same commit as the route handler. Models are never deferred to a later commit.

### R-A6: OpenAPI Accuracy

The auto-generated OpenAPI schema (FastAPI `/docs`) must remain accurate. No manual overrides. If the OpenAPI output is wrong, fix the Pydantic models — not the schema.

---

## Claude Code Procedure

When implementing any endpoint:
1. Create or update the `models.py` file in the target `api/` subdirectory
2. Define request and response models extending `BaseResponse`
3. Write the route handler referencing these models
4. Write a unit test asserting that malformed input is rejected with a structured error
5. Verify the FastAPI `/docs` page reflects the new endpoint correctly

---

## Verification Checklist

- [ ] Every route has a Pydantic request model
- [ ] Every route has a Pydantic response model extending `BaseResponse`
- [ ] No raw dicts cross module boundaries
- [ ] Invalid input returns structured `ErrorDetail`, not a 500
- [ ] Models file exists in the same `api/` subdirectory as the route
```

---

### File: `docs/skills/SKILL-B-governance.md`

```markdown
# SKILL-B: Memory & Architecture Governance

**Applies to**: Every increment, every file change, every PR.
**Authority**: This skill is referenced from CLAUDE.md Section 17.

---

## Purpose

Ensure Claude Code remains consistent with long-term architectural guidelines. This skill prompts refactoring, enforces folder structure, maintains documentation, and applies semantic versioning.

---

## Rules

### R-B1: CLAUDE.md Is the Constitution

Before generating or modifying any file, verify the change does not violate:
- CLAUDE.md Section 4 (Architecture Rules — non-negotiable)
- CLAUDE.md Section 5 (Session & Terminology)
- CLAUDE.md Section 6 (Identity & ID Conventions)
- CLAUDE.md Section 10 (UI State Machine)

If a proposed change conflicts with CLAUDE.md, the change is wrong — not CLAUDE.md.

### R-B2: Folder Structure Is Enforced

Every new file must land in the correct directory per CLAUDE.md Section 2:

| Content Type | Directory |
|---|---|
| UI components | `ui/` |
| Route handlers | `api/routers/` |
| Business logic | `api/services/` |
| Agent code | `api/agents/` |
| Graph interaction | `api/graph/` |
| Vector retrieval | `api/vector/` |
| Storage operations | `api/storage/` |
| Worker jobs | `api/worker/` |
| Schema models | `api/schema/` |
| Proposal models | `api/proposals/` |
| Diff builder | `api/diff/` |
| Audit writers | `api/audit/` |
| Prompt templates | `prompts/` |
| Test fixtures | `fixtures/` |
| Unit tests | `tests/unit/` |
| Integration tests | `tests/integration/` |
| Specifications | `docs/specs/` |
| Skills | `docs/skills/` |
| ADRs | `docs/adr/` |
| Infrastructure | `infra/` (read-only) |

A file in the wrong directory is a blocking issue.

### R-B3: Layer Violations Are Blockers

These imports are forbidden and must never appear:
- `ui/` importing from `api/graph/`, `api/agents/`, `api/vector/`, `api/diff/`, or `api/audit/`
- `api/vector/` or `api/agents/` (except Agent-C) importing write functions from `api/graph/`
- Any module importing `st.session_state` outside of `ui/state.py`

If Claude Code detects a layer violation during implementation, it must refactor before proceeding.

### R-B4: Documentation Updates Are Mandatory

After any increment that introduces:
- **A new module or directory** → Update CLAUDE.md Section 2 if the tree has changed
- **A new environment variable** → Add to CLAUDE.md Section 11.3
- **A new agent or pipeline component** → Update CLAUDE.md Section 9
- **A changed architectural decision** → Create an ADR in `docs/adr/NNN-title.md`

### R-B5: README.md Maintenance

`README.md` at the repo root must be updated at the end of every increment with:
- Current project status (which increments are complete)
- Setup instructions (reflecting current docker-compose and env vars)
- Available API endpoints (summary with link to `/docs`)
- Known limitations

### R-B6: Semantic Versioning

The `version` field in `pyproject.toml` follows semver:
- **PATCH** (0.x.Y): Bug fixes within an increment
- **MINOR** (0.X.0): Each completed increment (Increment 1 → 0.1.0, Increment 2 → 0.2.0, ...)
- **MAJOR** (X.0.0): Breaking change to API contracts or data model that invalidates existing runs

### R-B7: Refactoring Triggers

Claude Code must proactively refactor when:
- A module exceeds ~400 lines → split into focused sub-modules
- A function takes more than 5 parameters → introduce a params Pydantic model
- Duplicated logic appears in 2+ modules → extract to `api/common/` or shared service
- A Pydantic model is used across 3+ modules → move to `api/models/`

Refactoring is not optional polish — it is a governance requirement.

### R-B8: CLAUDE.md Updates for Spec/Skill References

When a new spec or skill document is created, CLAUDE.md Section 17 must be updated to reference it. Claude Code discovers which spec to follow by reading CLAUDE.md first.

---

## Post-Increment Governance Checklist

Run this after completing every increment:

- [ ] All new files are in correct directories per CLAUDE.md Section 2
- [ ] No `st.session_state` access outside `StateManager`
- [ ] No `uuid4()` for governed artifact IDs
- [ ] No business logic in `ui/`
- [ ] No direct graph writes outside the proposal pipeline (except Phase 3 extraction)
- [ ] No layer violations (check imports)
- [ ] All new endpoints have Pydantic request/response models (SKILL-A)
- [ ] `README.md` updated with current status
- [ ] `pyproject.toml` version bumped (MINOR for increment completion)
- [ ] CLAUDE.md updated if structure, env vars, or conventions changed
- [ ] All unit tests pass
- [ ] No modules exceed ~400 lines without a refactoring plan

---

## Claude Code Procedure

At the start of every task:
1. Read CLAUDE.md
2. Identify which increment spec applies (Section 17 references)
3. Read the relevant `docs/specs/SPEC-*.md`
4. Read all three `docs/skills/SKILL-*.md`
5. Verify the planned changes against R-B1 through R-B8
6. Implement
7. Run the post-increment governance checklist
8. Update README.md and CLAUDE.md as required
```

---

### File: `docs/skills/SKILL-C-packaging.md`

```markdown
# SKILL-C: Packaging Readiness

**Applies to**: All increments. No packaging is implemented — structure must support it.
**Authority**: This skill is referenced from CLAUDE.md Section 17.

---

## Purpose

The application will not be packaged within this project. However, the codebase must be structured so a packaging team can package it without refactoring. This skill defines the structural requirements.

---

## Rules

### R-C1: Entry Points

`pyproject.toml` must define entry points for:
- The API server (uvicorn)
- The ARQ worker

```toml
[project.scripts]
api-server = "api.main:run"
arq-worker = "api.worker.entry:run"
```

### R-C2: Absolute Imports Only

All imports must be absolute from the package root:
```python
# CORRECT
from api.services.ingestion import IngestorService
from api.graph.client import Neo4jClient

# BANNED — breaks outside the working directory
from .ingestion import IngestorService
from ..graph.client import Neo4jClient
```

### R-C3: Package Init Files

Every Python directory must contain an `__init__.py` file. Missing init files break package discovery.

### R-C4: Dependency Declarations

All dependencies must be declared in `pyproject.toml` with version bounds (`>=min,<max`). No undeclared transitive dependencies. If a module uses a library, that library must appear in `[project.dependencies]`.

### R-C5: Per-Service Dockerfiles

A `Dockerfile` must exist for each service:
- `Dockerfile.api` — FastAPI backend
- `Dockerfile.worker` — ARQ worker
- `Dockerfile.ui` — Streamlit frontend

These serve as both dev environment containers and the packaging team's starting point.

### R-C6: No Hardcoded Paths

No file path in the codebase may be hardcoded to a specific machine or container layout. All paths must be derived from environment variables or configuration.

---

## Verification Checklist

- [ ] `pyproject.toml` has `[project.scripts]` entry points
- [ ] All imports are absolute from package root
- [ ] Every Python directory has `__init__.py`
- [ ] All dependencies declared with version bounds
- [ ] Dockerfiles exist for api, worker, ui
- [ ] No hardcoded file paths
```

---

## Step 3: Generate Specification Documents

Generate one spec per increment. Each spec is self-contained: it describes what to build, which files to create, acceptance criteria, and which skills apply.

---

### File: `docs/specs/SPEC-01-scaffolding.md`

```markdown
# SPEC-01: Project Scaffolding & Session Lifecycle

**Increment**: 1
**Version target**: 0.1.0
**Prerequisites**: None
**Skills required**: SKILL-A, SKILL-B, SKILL-C

---

## Objective

Create the monorepo skeleton, Docker environment, configuration system, session model, StateManager, and minimal FastAPI + Streamlit shells. This increment produces a running but empty application.

---

## Specifications

### S-01.1: Repository Skeleton

Generate the full directory tree per CLAUDE.md Section 2. Every Python directory must contain `__init__.py` (SKILL-C R-C3).

### S-01.2: Project Definition

Create `pyproject.toml`:
- PEP 621 format
- Python 3.12 requirement
- All core dependencies with version bounds: FastAPI, uvicorn, Streamlit, Pydantic v2, ARQ, boto3, neo4j (driver), qdrant-client, httpx, structlog, ruff, mypy
- Entry points per SKILL-C R-C1
- Version: `0.1.0`

### S-01.3: Docker Environment

Create `docker-compose.yml` with services: FastAPI, Streamlit, Redis, RustFS, Qdrant. No Neo4j (Aura-only per CLAUDE.md Section 11.2).

Create per-service Dockerfiles: `Dockerfile.api`, `Dockerfile.worker`, `Dockerfile.ui` (SKILL-C R-C5).

### S-01.4: Configuration

Create `api/config.py`:
- Pydantic `Settings` model loading all env vars from CLAUDE.md Section 11.3
- Fail-closed: raise a clear error and refuse to start if any required var is missing (CLAUDE.md Section 4.4)
- Document which vars are required vs optional

### S-01.5: Session/Run Model

Create `api/models/run.py`:
- `Run` Pydantic model
- Deterministic `run_id` derivation: `hash(user_namespace + timestamp_seed)` — never `uuid4()` (CLAUDE.md Section 6)
- Fields: `run_id`, `schema_version` (initially null), `phase`, `created_at`

### S-01.6: StateManager

Create `ui/state.py`:
- `Phase` enum: INIT, SCHEMA, INGESTION, EXTRACTION, CURATION
- `StateManager` class: sole interface to `st.session_state` (CLAUDE.md Section 10.1)
- `advance_phase()`: enforces strict ordering — rejects skips and regressions
- Panel read/write mode tracking per phase

### S-01.7: Streamlit Shell

Create `ui/app.py`:
- Phase 0 screen: inputs for Neo4j URI, Neo4j user/password, OpenRouter API key
- Sidebar: displays current run state (run_id, phase, schema status)
- All state access through `StateManager` — no direct `st.session_state`

### S-01.8: FastAPI Shell

Create `api/main.py`:
- CORS configuration
- Startup hooks: validate Neo4j connectivity, Redis ping, S3 bucket existence, Qdrant health
- Shutdown hooks: clean connection pools

Create `api/routers/health.py`:
- `GET /api/health` — returns JSON with per-backend connectivity status
- Request and response models per SKILL-A

### S-01.9: README

Create `README.md`:
- Project overview (one paragraph)
- Setup instructions (env vars, docker compose up)
- Current status: Increment 1 complete

---

## Files to Generate

| Order | File Path | Purpose |
|-------|-----------|---------|
| 1 | `pyproject.toml` | Project definition |
| 2 | `README.md` | Project documentation |
| 3 | `docker-compose.yml` | Local dev environment |
| 4 | `Dockerfile.api` | API server container |
| 5 | `Dockerfile.worker` | ARQ worker container |
| 6 | `Dockerfile.ui` | Streamlit container |
| 7 | All `__init__.py` files | Package init files per CLAUDE.md Section 2 tree |
| 8 | `api/config.py` | Environment configuration |
| 9 | `api/models/run.py` | Run/session model |
| 10 | `api/main.py` | FastAPI application |
| 11 | `api/routers/health.py` | Health check endpoint |
| 12 | `ui/state.py` | StateManager |
| 13 | `ui/app.py` | Streamlit entry point |
| 14 | `tests/unit/test_run_id.py` | Deterministic ID test |
| 15 | `tests/unit/test_state_manager.py` | Phase enforcement test |

---

## Acceptance Criteria

- [ ] `docker compose up` starts all services without errors (given valid env vars)
- [ ] `GET /api/health` returns JSON with per-backend connectivity status
- [ ] StateManager rejects phase skipping in unit tests
- [ ] `run_id` is deterministic: same inputs → same output across repeated calls
- [ ] Missing required env var causes startup failure with clear error message
- [ ] `pyproject.toml` version is `0.1.0`
- [ ] `README.md` exists with setup instructions
- [ ] SKILL-B post-increment checklist passes
```

---

### File: `docs/specs/SPEC-02-schema.md`

```markdown
# SPEC-02: Domain Schema Definition (Phase 1)

**Increment**: 2
**Version target**: 0.2.0
**Prerequisites**: SPEC-01 complete
**Skills required**: SKILL-A, SKILL-B

---

## Objective

Implement Phase 1 of the workflow: the user describes a domain, the AI proposes node and edge types, the user reviews and approves, and the schema is locked for the remainder of the run.

---

## Specifications

### S-02.1: Schema Models

Create `api/schema/models.py`:
- `NodeTypeDef`: class, type, primary property, optional qualifier, additional properties list
- `EdgeTypeDef`: start node type, end node type, type, primary property, optional qualifier, additional properties list
- `SchemaVersion`: wraps approved node/edge type sets with an immutable version hash
- Version hash is deterministic: `hash(sorted canonical representation of all type definitions)`
- Post-approval, the schema object is frozen — modification attempts raise an error

### S-02.2: Schema Service

Create `api/schema/service.py`:
- `propose(domain_description: str, model: str) -> SchemaProposal`: calls OpenRouter, returns typed candidates
- `approve(proposal: SchemaProposal) -> SchemaVersion`: locks the schema, returns immutable version
- `get_current(run_id: str) -> SchemaVersion | None`: returns locked schema or None
- Post-lock, `propose()` and any modification method must raise `SchemaLockedError`

### S-02.3: LLM Client

Create `api/services/llm.py`:
- Typed OpenRouter client supporting model-per-job configuration
- Response validation: parse LLM output against expected Pydantic model
- Fail-closed: malformed output triggers structured fallback (log error, return fallback artifact) — never auto-correct or pass through

### S-02.4: Endpoints

Create `api/routers/schema.py`:
- `POST /api/schema/propose` — accepts `{run_id, domain_description, model?}`, returns `SchemaProposal`
- `POST /api/schema/approve` — accepts `{run_id, proposal_id}`, returns `SchemaVersion`
- `GET /api/schema/{run_id}` — returns locked `SchemaVersion` or 404
- All endpoints use SKILL-A request/response models

### S-02.5: Prompt Template

Create `prompts/schema_propose/v1.yaml`:
- `job_id`: `schema_propose`
- `template_version`: `v1`
- System prompt defining the task
- User template with `{domain_description}` placeholder
- Expected output schema reference (the Pydantic model the response must conform to)
- No inline prompts anywhere in code — all agent code references this template file

### S-02.6: UI

Create `ui/pages/schema.py`:
- Phase 1 panel (only accessible when `StateManager.phase == SCHEMA`)
- Text input for domain description
- Button to trigger proposal
- Editable table displaying proposed node types and edge types
- Approve button → locks schema → advances phase to INGESTION

---

## Files to Generate

| Order | File Path | Purpose |
|-------|-----------|---------|
| 1 | `api/schema/models.py` | Schema Pydantic models |
| 2 | `api/schema/service.py` | Schema lifecycle logic |
| 3 | `api/services/llm.py` | OpenRouter client |
| 4 | `api/routers/schema.py` | Schema endpoints |
| 5 | `prompts/schema_propose/v1.yaml` | Prompt template |
| 6 | `ui/pages/schema.py` | Phase 1 UI panel |
| 7 | `tests/unit/test_schema_models.py` | Schema version determinism |
| 8 | `tests/unit/test_schema_lock.py` | Post-lock rejection |
| 9 | `tests/unit/test_llm_validation.py` | Malformed response handling |

---

## Acceptance Criteria

- [ ] `POST /api/schema/propose` returns typed node/edge candidates
- [ ] `POST /api/schema/approve` locks schema; subsequent modifications rejected
- [ ] Schema version hash is deterministic across repeated computations
- [ ] Malformed LLM response triggers fail-closed fallback, not a crash or pass-through
- [ ] Prompt loaded from `prompts/schema_propose/v1.yaml` — no inline prompt strings
- [ ] UI enforces phase ordering via StateManager
- [ ] `pyproject.toml` version is `0.2.0`
- [ ] SKILL-B post-increment checklist passes
```

---

### File: `docs/specs/SPEC-03-ingestion.md`

```markdown
# SPEC-03: Document Ingestion & Chunking (Phase 2)

**Increment**: 3
**Version target**: 0.3.0
**Prerequisites**: SPEC-02 complete
**Skills required**: SKILL-A, SKILL-B

---

## Objective

Implement Phase 2: users upload documents, the system parses and chunks them with a three-tier fallback chain, indexes chunks in Qdrant, and generates a manifest for incremental reruns.

---

## Specifications

### S-03.1: Parser Fallback Chain

Create `api/services/ingestion.py` — `IngestorService` with three-tier parsing:

1. **Primary — Docling**: Full structural parsing (titles, paragraphs, tables, images, captions). Preferred parser.
2. **Secondary — Unstructured**: Activated if Docling raises an exception or returns an empty/null result for a document.
3. **Tertiary — Raw text extraction**: PyPDF2 (PDF) or python-docx (DOCX). Produces flat text with no structural metadata. All chunks receive `quality_flag: "raw_fallback"`. Activated if both Docling and Unstructured fail.

Fallback behavior:
- Each transition is logged with the reason (exception type, empty result indicator)
- The parser that succeeded is recorded in the manifest as `parser_used`
- If all three fail, the document is hard-rejected with a structured error — never silently skipped
- Parsers are individually toggleable via `api/config.py`

### S-03.2: Chunking

Create `api/services/chunking.py`:
- Semantic grouping respecting heading/paragraph continuity
- Tables isolated as standalone chunks
- Configurable character and token limits (via `api/config.py`)
- Each `chunk_id = hash(doc_id + start_page_locator)` — deterministic (CLAUDE.md Section 6)
- Provenance validation:
  - **Hard-reject**: missing chunk_id or missing start_page_locator → chunk not created
  - **Soft-flag**: low OCR confidence or low text density → chunk created with `quality_flag`

### S-03.3: Document & Chunk Models

Create `api/models/document.py`:
- `Document`: doc_id, run_id, source_identity, content_hash, parser_used, status
- `Chunk`: chunk_id, doc_id, run_id, start_page_locator, end_page_locator, text, structural_type, quality_flags[]
- `DocumentManifest`: doc_hash, parser_config_hash, parser_used, chunk_ids[], quality_summary, timestamp
- `ChunkQualityFlag`: enum (low_ocr_confidence, low_text_density, raw_fallback)

### S-03.4: Storage

Create `api/storage/artifacts.py`:
- boto3 wrapper for S3-compatible storage
- `store_raw_document(run_id, doc_id, content: bytes)`
- `store_manifest(run_id, doc_id, manifest: DocumentManifest)`
- `retrieve_manifest(run_id, doc_id) -> DocumentManifest | None`
- Manifest enables incremental reruns: if content_hash matches existing manifest, skip reprocessing

### S-03.5: Vector Indexing

Create `api/vector/indexer.py`:
- Embed chunk text (via OpenRouter embedding endpoint or sentence-transformers)
- Upsert to Qdrant with metadata: chunk_id, doc_id, run_id, page locator, quality_flags
- This is evidence-only storage — never authoritative (CLAUDE.md Section 4.1)

### S-03.6: Endpoints

Create `api/routers/documents.py`:
- `POST /api/documents/ingest` — file upload, returns doc_id and ingestion status
- `GET /api/documents/{run_id}` — list documents with status and parser_used
- `GET /api/documents/{run_id}/{doc_id}/chunks` — list chunks with quality flags
- All endpoints use SKILL-A models

### S-03.7: UI

Create `ui/pages/ingestion.py`:
- Phase 2 panel
- File upload widget (PDF, DOCX)
- Ingestion progress per document (parsing, chunking, indexing)
- Chunk manifest review with quality flag indicators (warnings for raw_fallback)
- Advance to Phase 3 when at least one document is fully ingested

---

## Files to Generate

| Order | File Path | Purpose |
|-------|-----------|---------|
| 1 | `api/models/document.py` | Document/chunk models |
| 2 | `api/services/ingestion.py` | Parser fallback chain |
| 3 | `api/services/chunking.py` | Chunking logic |
| 4 | `api/storage/artifacts.py` | S3 artifact storage |
| 5 | `api/vector/indexer.py` | Qdrant chunk indexing |
| 6 | `api/routers/documents.py` | Ingestion endpoints |
| 7 | `ui/pages/ingestion.py` | Phase 2 UI |
| 8 | `fixtures/sample.pdf` | Minimal test PDF |
| 9 | `fixtures/sample.docx` | Minimal test DOCX |
| 10 | `tests/unit/test_doc_id.py` | Deterministic doc_id |
| 11 | `tests/unit/test_chunk_id.py` | Deterministic chunk_id |
| 12 | `tests/unit/test_chunking.py` | Chunking boundaries |
| 13 | `tests/unit/test_ingestion_fallback.py` | Fallback chain behavior |
| 14 | `tests/unit/test_manifest.py` | Manifest idempotency |

---

## Acceptance Criteria

- [ ] Docling successfully parses test documents with structural metadata
- [ ] Mocked Docling failure activates Unstructured transparently
- [ ] Mocked Docling + Unstructured failure activates raw fallback with `quality_flag: "raw_fallback"`
- [ ] All three parser failures → hard-reject with structured error
- [ ] `parser_used` in manifest correctly reflects which parser succeeded
- [ ] doc_id and chunk_id are deterministic; manifest is idempotent on rerun
- [ ] Chunks indexed in Qdrant with correct metadata
- [ ] `pyproject.toml` version is `0.3.0`
- [ ] SKILL-B post-increment checklist passes
```

---

### File: `docs/specs/SPEC-04-extraction.md`

```markdown
# SPEC-04: AI-Assisted Extraction (Phase 3)

**Increment**: 4
**Version target**: 0.4.0
**Prerequisites**: SPEC-03 complete
**Skills required**: SKILL-A, SKILL-B

---

## Objective

Implement Phase 3: chunks are sent to the LLM with the locked schema, extracted nodes and edges are validated and written to Neo4j. This is the one phase where writes go directly to the graph (no proposal pipeline).

---

## Specifications

### S-04.1: ARQ Worker

Create `api/worker/entry.py`: ARQ worker entry point. Configures Redis connection, imports job functions.
Create `api/worker/jobs.py`: `extraction_job(run_id, chunk_id)` — processes one chunk.

### S-04.2: Extraction Service

Create `api/services/extraction.py`:
- Sends chunk text + locked schema structure to OpenRouter
- Uses prompt template from `prompts/extraction/v1.yaml`
- Validates response against `ExtractionResult` Pydantic model
- Fail-closed: malformed output blocked and logged (CLAUDE.md Section 4.4)

### S-04.3: Extraction Models

Create `api/models/extraction.py`:
- `ExtractedNode`: node_type, primary_property, qualifier, additional_properties, dedupe_key, source_chunk_id
- `ExtractedEdge`: edge_type, start_node_dedupe_key, end_node_dedupe_key, primary_property, qualifier, source_chunk_id
- `ExtractionResult`: run_id, chunk_id, nodes[], edges[], schema_version

### S-04.4: Graph Writer

Create `api/graph/client.py`: Neo4j driver wrapper — connection pooling, session management, retry logic.
Create `api/graph/writer.py`: Parameterized Cypher only (CLAUDE.md Section 4.2). Writes nodes/edges with dedupe keys. Duplicate prevention via MERGE on dedupe_key.

### S-04.5: Endpoints

Create `api/routers/extraction.py`:
- `POST /api/extraction/run` — enqueues extraction jobs for all chunks in the run
- `GET /api/extraction/status/{run_id}` — per-chunk job progress
- `GET /api/extraction/results/{run_id}` — extraction summary (node/edge counts by type)

### S-04.6: Prompt Template

Create `prompts/extraction/v1.yaml`: extraction prompt with chunk text + schema placeholders.

### S-04.7: UI

Create `ui/pages/extraction.py`: Phase 3 panel — trigger extraction, job progress, summary, entity preview.

---

## Files to Generate

| Order | File Path | Purpose |
|-------|-----------|---------|
| 1 | `api/models/extraction.py` | Extraction result models |
| 2 | `api/services/extraction.py` | Extraction logic |
| 3 | `api/graph/client.py` | Neo4j client wrapper |
| 4 | `api/graph/writer.py` | Parameterized graph writes |
| 5 | `api/worker/entry.py` | ARQ entry point |
| 6 | `api/worker/jobs.py` | Extraction job definition |
| 7 | `api/routers/extraction.py` | Extraction endpoints |
| 8 | `prompts/extraction/v1.yaml` | Extraction prompt |
| 9 | `ui/pages/extraction.py` | Phase 3 UI |
| 10 | `tests/unit/test_extraction_validation.py` | Output validation |
| 11 | `tests/unit/test_dedupe_keys.py` | Dedupe key derivation |
| 12 | `tests/unit/test_worker_jobs.py` | Job serialization |
| 13 | `tests/integration/test_graph_write.py` | Neo4j write + dedupe |

---

## Acceptance Criteria

- [ ] Extraction jobs enqueue and execute in ARQ worker
- [ ] Extracted entities conform to locked schema types
- [ ] Malformed LLM output is rejected (fail-closed, not auto-corrected)
- [ ] Nodes/edges written to Neo4j with MERGE on dedupe keys
- [ ] Rerun on same chunks does not create duplicates
- [ ] `pyproject.toml` version is `0.4.0`
- [ ] SKILL-B post-increment checklist passes
```

---

### File: `docs/specs/SPEC-05-candidates.md`

```markdown
# SPEC-05: Deterministic Candidate Generation (Curation Layer 1)

**Increment**: 5
**Version target**: 0.5.0
**Prerequisites**: SPEC-04 complete
**Skills required**: SKILL-A, SKILL-B

---

## Objective

Implement the pre-curation layer: deterministic, zero-LLM detection of duplicates, violations, and anomalies in the extracted graph.

---

## Specifications

### S-05.1: Candidate Detectors

Create `api/services/curation/candidates.py` — five detector classes, all deterministic:

| Detector | Logic | LLM? |
|----------|-------|------|
| Exact node duplicate | Same NodeType + same dedupe_key + different IDs | No |
| Exact relationship duplicate | Same type + same start/end + same identity key | No |
| Probable duplicate | Blocking keys + Jaro-Winkler similarity + shared context score | No |
| Canonical/inverse violation | Schema-defined direction and inverse mapping rules | No |
| Structural anomaly | Orphan nodes, degree outliers, missing provenance, qualifier issues | No |

### S-05.2: Candidate Model

Create `api/models/candidate.py`:
- `candidate_id = hash(candidate_inputs + detection_method)` — deterministic
- Fields: candidate_type, candidate_lane, collision_context, involved_element_refs[], severity, detection_method

### S-05.3: Graph Reader

Create `api/graph/reader.py`: Read-only Neo4j queries for candidate detection — neighbor traversal, orphan detection, degree computation, dedupe key lookups.

### S-05.4: Endpoints

Update `api/routers/curation.py`:
- `POST /api/curation/candidates/generate` — runs all detectors
- `GET /api/curation/candidates/{run_id}` — list candidates grouped by type

### S-05.5: UI

Create `ui/pages/curation.py`: Phase 4 entry — candidate list grouped by type with severity indicators.

---

## Files to Generate

| Order | File Path | Purpose |
|-------|-----------|---------|
| 1 | `api/models/candidate.py` | Candidate model |
| 2 | `api/services/curation/candidates.py` | Five detectors |
| 3 | `api/graph/reader.py` | Read-only graph queries |
| 4 | `api/routers/curation.py` | Candidate endpoints |
| 5 | `ui/pages/curation.py` | Phase 4 Layer 1 UI |
| 6 | `fixtures/candidate_detection/*.json` | Fixture graph states per detector |
| 7 | `tests/unit/test_candidate_*.py` | Per-detector tests + ID determinism |

---

## Acceptance Criteria

- [ ] All five detectors produce correct candidates from fixture data
- [ ] Candidate IDs are deterministic
- [ ] Zero LLM involvement in any detection path
- [ ] `pyproject.toml` version is `0.5.0`
- [ ] SKILL-B post-increment checklist passes
```

---

### File: `docs/specs/SPEC-06-manual-curation.md`

```markdown
# SPEC-06: Manual Curation, Evidence Retrieval & Proposal Pipeline (Layer 2)

**Increment**: 6
**Version target**: 0.6.0
**Prerequisites**: SPEC-05 complete
**Skills required**: SKILL-A, SKILL-B

---

## Objective

Implement the full governed mutation pipeline (propose → approve → diff → execute → audit), evidence retrieval for manual decision-making, and a paginated graph explorer for browsing all nodes and edges.

---

## Specifications

### S-06.1: Proposal Packet

Create `api/proposals/models.py`: Full model per CLAUDE.md Section 7.
Create `api/proposals/service.py`: Lifecycle — create, list, transition states (pending → approved/rejected/deferred).

### S-06.2: Deterministic Diff Builder

Create `api/diff/models.py`: `DiffPlan`, `DiffOperation` enum (create_node, update_node, delete_node, create_edge, update_edge, delete_edge, merge_nodes).
Create `api/diff/builder.py`: Non-LLM. Proposal Packet → structured diff. `diff_id = hash(diff_content)`. Same inputs → same diff.

### S-06.3: Approval Gate

Create `api/routers/approvals.py`:
- `POST /api/curation/proposals/{id}/approve` — issues approval_id
- `POST .../reject`, `POST .../defer`
- High-risk (merge, delete): two-phase approval (phase1 → confirm)

### S-06.4: Execution Agent (Agent-C)

Create `api/agents/execution.py`: Tools-only, no reasoning. Validates approval_id, schema version match. Typed graph mutations. Post-apply invariant checks. Appends immutable audit record.

### S-06.5: Audit Log

Create `api/audit/models.py`: `AuditRecord` model.
Create `api/audit/writer.py`: Immutable append-only records to S3.

### S-06.6: Evidence Retrieval

Create `api/vector/retriever.py`: Queries Qdrant by dedupe key, source doc, and semantic similarity. Returns ranked chunks with text, source, page locator, relevance score.
Create `api/routers/evidence.py`:
- `GET /api/curation/evidence/{candidate_id}` — evidence for a candidate
- `POST /api/curation/evidence/query` — ad-hoc evidence query by node/edge reference

### S-06.7: Graph Explorer (Paginated)

Create `api/routers/graph_explorer.py`:
- `GET /api/graph/nodes/{run_id}?page=1&page_size=50&node_type=...` — paginated nodes (max 50/page), filterable by type
- `GET /api/graph/edges/{run_id}?page=1&page_size=50&edge_type=...` — paginated edges
- `GET /api/graph/nodes/{run_id}/count`, `GET /api/graph/edges/{run_id}/count` — totals by type

### S-06.8: Manual Curation Endpoints

Update `api/routers/curation.py`:
- `POST /api/curation/propose` — user creates a proposal (same pipeline as AI, no bypass)
- `GET /api/curation/proposals/{run_id}` — list proposals with status
- `POST /api/curation/proposals/{id}/execute` — after approval, triggers diff → Agent-C → audit

### S-06.9: UI

Update `ui/pages/curation.py`: candidate detail with evidence view, proposal form, approval queue, execution status, audit trail.
Create `ui/pages/graph_explorer.py`: scrollable node and edge tables (50 rows/page), filterable by type, with pagination controls. Read-only — users browse here, act in curation panel.

---

## Files to Generate

| Order | File Path | Purpose |
|-------|-----------|---------|
| 1 | `api/proposals/models.py` | Proposal Packet model |
| 2 | `api/proposals/service.py` | Proposal lifecycle |
| 3 | `api/diff/models.py` | Diff models |
| 4 | `api/diff/builder.py` | Deterministic diff builder |
| 5 | `api/audit/models.py` | Audit record model |
| 6 | `api/audit/writer.py` | Immutable audit writer |
| 7 | `api/agents/execution.py` | Agent-C |
| 8 | `api/vector/retriever.py` | Evidence retrieval |
| 9 | `api/routers/evidence.py` | Evidence endpoints |
| 10 | `api/routers/approvals.py` | Approval gate endpoints |
| 11 | `api/routers/graph_explorer.py` | Paginated graph browse |
| 12 | `api/routers/curation.py` | Manual curation endpoints (update) |
| 13 | `ui/pages/curation.py` | Curation UI (update) |
| 14 | `ui/pages/graph_explorer.py` | Graph explorer UI |
| 15 | `tests/unit/test_diff_determinism.py` | Diff reproducibility |
| 16 | `tests/unit/test_proposal_lifecycle.py` | Proposal state transitions |
| 17 | `tests/unit/test_approval_gate.py` | Approval validation |
| 18 | `tests/unit/test_audit_record.py` | Audit record structure |
| 19 | `tests/unit/test_evidence_retrieval.py` | Evidence query behavior |
| 20 | `tests/unit/test_graph_explorer_pagination.py` | Pagination correctness |
| 21 | `tests/integration/test_manual_curation_cycle.py` | End-to-end cycle |
| 22 | `fixtures/curation/sample_proposal.json` | Test proposal |
| 23 | `fixtures/curation/sample_diff.json` | Test diff |

---

## Acceptance Criteria

- [ ] Full cycle: candidate → evidence → propose → diff preview → approve → execute → audit
- [ ] Evidence retrieval returns relevant Qdrant chunks with source/page metadata
- [ ] Diff determinism: same proposal → same diff_id
- [ ] Execution rejected without valid approval_id
- [ ] Two-phase approval enforced for merge/delete proposals
- [ ] Graph explorer returns paginated nodes/edges (50/page) with type filtering
- [ ] Audit records immutable in S3
- [ ] `pyproject.toml` version is `0.6.0`
- [ ] SKILL-B post-increment checklist passes
```

---

### File: `docs/specs/SPEC-07-agent-pipeline.md`

```markdown
# SPEC-07: AI-Powered Curation Agent Pipeline (Layer 3)

**Increment**: 7
**Version target**: 0.7.0
**Prerequisites**: SPEC-06 complete
**Skills required**: SKILL-A, SKILL-B

---

## Objective

Implement the multi-agent curation pipeline: Orchestrator → Agent-A → Agent-B → Agent-P → Diff Builder → Human Approval → Agent-C. All AI proposals flow through the same governed pipeline as manual curation.

---

## Specifications

### S-07.1: Orchestrator

Create `api/agents/orchestrator.py`: Non-LLM. Assigns risk class (low/medium/high) and tool budget (token limit, cost limit, max retrieval rounds) per candidate. Routes into agent pipeline. Loop guards prevent runaway processing.

### S-07.2: Agent-A (Evidence Assembly)

Create `api/agents/evidence.py`: LLM agent. Receives candidate + graph context. Retrieves chunks from Qdrant via `api/vector/retriever.py`. Classifies evidence (Supporting, Corroborating, Conflicting). Computes sufficiency score. Output: typed `EvidenceReport`. Does not decide actions.

### S-07.3: Agent-B (Retrieval Augmentation)

Create `api/agents/retrieval.py`: LLM agent. Triggered only if Agent-A flags insufficient evidence. Targeted retrieval within budget. Loop-guarded (max N rounds). If still insufficient → defer.

### S-07.4: Agent-P (Proposal Composer)

Create `api/agents/proposal.py`: LLM agent. Receives candidate + evidence report. Selects proposal class, cites rule IDs and evidence IDs, writes rationale. Outputs Proposal Packet. Never builds diffs or executes.

### S-07.5: Agent Output Contracts

Create `api/agents/models.py`: `EvidenceReport`, `RetrievalResult`, `AgentProposalOutput` — typed output contracts validated on every agent response. Malformed → fail-closed.

### S-07.6: Prompt Templates

Create `prompts/evidence_assembly/v1.yaml`, `prompts/retrieval_augmentation/v1.yaml`, `prompts/proposal_composer/v1.yaml`.

### S-07.7: Pipeline Jobs

Update `api/worker/jobs.py`: `evidence_assembly_job`, `retrieval_augmentation_job`, `proposal_composition_job`. Chained execution.

### S-07.8: Model-Per-Job Config

Extend `api/config.py`: per-agent model assignment, token/cost budgets, reranking top-N.

### S-07.9: UI

Update `ui/pages/curation.py`: batch candidate processing, agent chain progress, evidence reports, AI proposals with rationale, pending approval queue.

---

## Files to Generate

| Order | File Path | Purpose |
|-------|-----------|---------|
| 1 | `api/agents/models.py` | Agent output contracts |
| 2 | `api/agents/orchestrator.py` | Orchestrator |
| 3 | `api/agents/evidence.py` | Agent-A |
| 4 | `api/agents/retrieval.py` | Agent-B |
| 5 | `api/agents/proposal.py` | Agent-P |
| 6 | `prompts/evidence_assembly/v1.yaml` | Agent-A prompt |
| 7 | `prompts/retrieval_augmentation/v1.yaml` | Agent-B prompt |
| 8 | `prompts/proposal_composer/v1.yaml` | Agent-P prompt |
| 9 | `api/worker/jobs.py` | Pipeline jobs (update) |
| 10 | `api/config.py` | Per-agent config (update) |
| 11 | `ui/pages/curation.py` | Layer 3 UI (update) |
| 12 | `tests/unit/test_orchestrator_*.py` | Risk class, budget, loop guard |
| 13 | `tests/unit/test_agent_output_contracts.py` | Contract validation |
| 14 | `tests/unit/test_agent_fallback.py` | Fallback on failure |
| 15 | `fixtures/agent_pipeline/*.json` | Sample candidates, evidence |

---

## Acceptance Criteria

- [ ] Orchestrator assigns risk class and budgets correctly
- [ ] Agent-A produces typed evidence report; malformed → fallback
- [ ] Agent-B only triggers on insufficient evidence; respects loop guard
- [ ] Agent-P produces valid Proposal Packet without Cypher or executable instructions
- [ ] Full pipeline: candidate → agents → proposal → diff → approval queue
- [ ] Budget enforcement prevents token overrun
- [ ] `pyproject.toml` version is `0.7.0`
- [ ] SKILL-B post-increment checklist passes
```

---

### File: `docs/specs/SPEC-08-hardening.md`

```markdown
# SPEC-08: Polish, Observability & Hardening

**Increment**: 8
**Version target**: 0.8.0
**Prerequisites**: SPEC-07 complete
**Skills required**: SKILL-A, SKILL-B, SKILL-C

---

## Objective

Harden the application: dry-run mode, CI pipeline, structured logging, error handling audit, UI polish, and final documentation pass.

---

## Specifications

### S-08.1: Dry-Run Mode

Add `DRY_RUN` flag to `api/config.py`. When true, Agent-C skips graph mutations. Diffs and proposals still generated and logged to S3.

### S-08.2: CI Pipeline

Create `.github/workflows/ci.yml`: lint (ruff), type check (mypy), unit tests, integration tests (skipped without Aura env vars), Docker build.

### S-08.3: Structured Logging

Create `api/logging.py`: structlog configuration. JSON output. Request correlation IDs tied to `run_id`. Agent pipeline telemetry (per-step timing, token usage, evidence scores).

### S-08.4: Error Handling Audit

Review all endpoints and agent jobs for fail-closed compliance. Ensure no silent coercion. Add missing fallback paths.

### S-08.5: UI Polish

Create `ui/pages/dashboard.py`: run summary, graph statistics.
Create `ui/components/phase_indicator.py`: progress indicator widget.
Add audit trail export (JSON) to curation UI.

### S-08.6: ADRs

Create `docs/adr/001-parser-fallback-chain.md`, `docs/adr/002-no-local-neo4j.md`, `docs/adr/003-deterministic-ids.md`.

### S-08.7: Final Documentation

Update `README.md` comprehensively. Update `CLAUDE.md` with any new conventions.

---

## Files to Generate

| Order | File Path | Purpose |
|-------|-----------|---------|
| 1 | `api/config.py` | Add DRY_RUN (update) |
| 2 | `api/logging.py` | Structured logging |
| 3 | `.github/workflows/ci.yml` | CI pipeline |
| 4 | `ui/pages/dashboard.py` | Run summary |
| 5 | `ui/components/phase_indicator.py` | Progress widget |
| 6 | `docs/adr/001-parser-fallback-chain.md` | ADR |
| 7 | `docs/adr/002-no-local-neo4j.md` | ADR |
| 8 | `docs/adr/003-deterministic-ids.md` | ADR |
| 9 | `tests/unit/test_dry_run.py` | Dry-run behavior |
| 10 | `README.md` | Final version |
| 11 | `CLAUDE.md` | Final update |

---

## Acceptance Criteria

- [ ] Dry-run mode produces proposals/diffs without graph mutations
- [ ] CI passes: lint, type check, unit tests, Docker build
- [ ] Structured logs with correlation IDs and agent telemetry
- [ ] All fail-closed paths verified
- [ ] `pyproject.toml` version is `0.8.0`
- [ ] `README.md` and `CLAUDE.md` finalized
- [ ] SKILL-B post-increment checklist passes
```

---

## Step 4: Update CLAUDE.md

This is the critical step. CLAUDE.md must be updated so that Claude Code discovers all specs and skills by reading it. Two sections need changes:

### 4A: Update Section 2 (Repository Structure)

Add `docs/` to the tree:

**Replace** the existing tree block with:

```
/
├── ui/                  # Streamlit frontend (pure UI layer only)
├── api/                 # FastAPI backend (all business logic)
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
│   └── audit/           # Immutable audit log writers
├── docs/                # Governance artifacts (specs, skills, ADRs)
│   ├── specs/           # Increment specification documents (SPEC-01 through SPEC-08)
│   ├── skills/          # Cross-cutting skill definitions (SKILL-A, B, C)
│   └── adr/             # Architecture Decision Records
├── prompts/             # Versioned prompt templates, keyed by job ID
├── fixtures/            # Test fixture inputs for deterministic components
├── tests/
│   ├── unit/            # Deterministic component tests (no network)
│   └── integration/     # Neo4j Aura integration tests (requires env vars)
├── infra/               # IaC and deployment files (read context only)
├── docker-compose.yml   # Local dev environment
├── pyproject.toml       # Single PEP 621 project definition
├── README.md            # Project documentation
└── CLAUDE.md            # This file
```

### 4B: Replace Section 17 (Skills)

**Replace** the placeholder content in Section 17 with:

```markdown
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

Each spec defines what to build, which files to create, and acceptance criteria for one increment. Increments are strictly ordered — do not start N+1 until N passes.

| Spec | Increment | Description |
|------|-----------|-------------|
| [SPEC-01](docs/specs/SPEC-01-scaffolding.md) | 1 | Project scaffolding & session lifecycle |
| [SPEC-02](docs/specs/SPEC-02-schema.md) | 2 | Domain schema definition (Phase 1) |
| [SPEC-03](docs/specs/SPEC-03-ingestion.md) | 3 | Document ingestion & chunking (Phase 2) |
| [SPEC-04](docs/specs/SPEC-04-extraction.md) | 4 | AI-assisted extraction (Phase 3) |
| [SPEC-05](docs/specs/SPEC-05-candidates.md) | 5 | Deterministic candidate generation (Curation Layer 1) |
| [SPEC-06](docs/specs/SPEC-06-manual-curation.md) | 6 | Manual curation, evidence retrieval & proposal pipeline (Layer 2) |
| [SPEC-07](docs/specs/SPEC-07-agent-pipeline.md) | 7 | AI-powered curation agent pipeline (Layer 3) |
| [SPEC-08](docs/specs/SPEC-08-hardening.md) | 8 | Polish, observability & hardening |

### 17.3 Cross-Cutting Skills

Skills define behavioral rules that apply across all increments. Claude Code must read and follow all skills during every implementation task.

| Skill | Scope | Description |
|-------|-------|-------------|
| [SKILL-A](docs/skills/SKILL-A-api-contracts.md) | Every endpoint | Pydantic request/response models, validation at boundaries |
| [SKILL-B](docs/skills/SKILL-B-governance.md) | Every change | Folder structure, layer enforcement, doc updates, semver, refactoring |
| [SKILL-C](docs/skills/SKILL-C-packaging.md) | All increments | Entry points, absolute imports, init files, dependency declarations |
```

### 4C: Update Section 18 (What Claude Should Always Do)

**Add** these two items to the existing list:

```markdown
- Before implementing any increment, read the relevant spec in `docs/specs/` and all skills in `docs/skills/`
- After completing any increment, run the SKILL-B governance checklist and update README.md and CLAUDE.md
```

---

## Step 5: Execution Summary

Claude Code should execute these steps in order:

| Step | Action | Files Created/Modified |
|------|--------|----------------------|
| 1 | Create directory structure | `docs/specs/`, `docs/skills/`, `docs/adr/` |
| 2 | Generate SKILL-A | `docs/skills/SKILL-A-api-contracts.md` |
| 3 | Generate SKILL-B | `docs/skills/SKILL-B-governance.md` |
| 4 | Generate SKILL-C | `docs/skills/SKILL-C-packaging.md` |
| 5 | Generate SPEC-01 | `docs/specs/SPEC-01-scaffolding.md` |
| 6 | Generate SPEC-02 | `docs/specs/SPEC-02-schema.md` |
| 7 | Generate SPEC-03 | `docs/specs/SPEC-03-ingestion.md` |
| 8 | Generate SPEC-04 | `docs/specs/SPEC-04-extraction.md` |
| 9 | Generate SPEC-05 | `docs/specs/SPEC-05-candidates.md` |
| 10 | Generate SPEC-06 | `docs/specs/SPEC-06-manual-curation.md` |
| 11 | Generate SPEC-07 | `docs/specs/SPEC-07-agent-pipeline.md` |
| 12 | Generate SPEC-08 | `docs/specs/SPEC-08-hardening.md` |
| 13 | Update CLAUDE.md Section 2 | Add `docs/` to repository structure tree |
| 14 | Update CLAUDE.md Section 17 | Replace placeholder with spec/skill reference table |
| 15 | Update CLAUDE.md Section 18 | Add spec/skill reading directives |

**After Step 15, CLAUDE.md is the single entry point.** Claude Code reads it, finds the references, follows them, and implements accordingly. No other coordination document is needed.
