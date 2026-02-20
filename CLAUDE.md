# CLAUDE.md — AI-Powered Graph Extraction & Curation Platform

Read this file fully before writing any code or making architectural decisions.

---

## 1. Project Purpose

A **session-based, AI-assisted web application** that transforms documents into a curated knowledge graph in Neo4j. Every graph mutation flows through: `Proposal → Human Approval → Deterministic Diff → Execution → Audit`. AI interprets evidence but cannot directly mutate state.

---

## 2. Repository Structure

```
/
├── ui/                  # Streamlit frontend (UI layer only — no business logic)
├── api/                 # FastAPI backend (all business logic)
│   ├── routers/         # HTTP route handlers
│   ├── services/        # Domain services (ingestion, extraction, curation)
│   ├── agents/          # Agent components (typed, constrained)
│   ├── graph/           # Neo4j interaction (sole write authority)
│   ├── vector/          # Qdrant retrieval (evidence-only, no writes)
│   ├── storage/         # Artifact storage via boto3
│   ├── worker/          # ARQ worker entry point
│   ├── schema/          # Domain schema definitions
│   ├── proposals/       # Proposal Packet models
│   ├── diff/            # Deterministic diff builder (non-LLM)
│   └── audit/           # Immutable audit log writers
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
| Agent-B | LLM | Budgeted retrieval (loop-guarded) |
| Agent-P | LLM | Compose Proposal Packet only |
| Diff Builder | Non-LLM | Translate proposal → deterministic diff |
| Approval Gate | Human | Approve / Reject / Defer |
| Agent-C | Tools-only | Apply approved diff (no reasoning) |

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

## 15. Skills

<!-- PLACEHOLDER: Task-specific skill definitions to be added. -->
<!-- Check this section before implementing governed pipeline components. -->

---

## 16. Claude Directives

### Always Do
- Read this file before writing code
- Derive IDs deterministically
- Route all writes through approval pipeline
- Use `StateManager` for session state
- Reference versioned prompt templates
- Fail closed on invalid inputs
- Test fallback behavior, not model quality

### Never Do
- Write Cypher in LLM agents
- Allow non-Agent-C graph mutations
- Access `st.session_state` directly
- Use `uuid4()` for governed IDs
- Silently coerce invalid payloads
- Bypass approval gate
- Auto-apply infra changes
- Write artifacts to disk or Neo4j/Qdrant
