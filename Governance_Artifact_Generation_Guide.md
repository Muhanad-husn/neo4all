# Governance Artifact Generation Guide

**This is a one-time instruction file.** It tells Claude Code to place the governance artifacts into the repo and update `CLAUDE.md`. After execution, delete this file. `CLAUDE.md` becomes the sole entry point.

---

## What Claude Code Must Do

### Step 1: Create directories

```bash
mkdir -p docs/specs docs/skills docs/adr
touch docs/adr/.gitkeep
```

### Step 2: Place the skill files

Copy these files into `docs/skills/`:

| File | Description |
|------|-------------|
| `SKILL-A-api-contracts.md` | Pydantic models at every API boundary, validation at edges |
| `SKILL-B-governance.md` | Folder structure, layer enforcement, semver, refactoring triggers, post-increment checklist |
| `SKILL-C-packaging.md` | Entry points, absolute imports, init files, dependency declarations, Dockerfiles |
| `SKILL-D-observability.md` | Centralized logging (structlog), Redis cache, monitoring endpoints + UI dashboard |

### Step 3: Place the spec files

Copy these files into `docs/specs/`:

| File | Inc | Phase | Key Additions |
|------|-----|-------|---------------|
| `SPEC-01-scaffolding.md` | 1 | 0 | Skeleton, Docker, config, StateManager, **logging, cache, monitoring** |
| `SPEC-02-schema.md` | 2 | 1 | Schema propose/approve/lock, OpenRouter client, prompt templates |
| `SPEC-03-ingestion.md` | 3 | 2 | Docling → Unstructured → raw fallback, chunking, Qdrant indexing |
| `SPEC-04-extraction.md` | 4 | 3 | ARQ worker, LLM extraction, Neo4j writes, **worker monitoring** |
| `SPEC-05-candidates.md` | 5 | 4.1 | Five deterministic detectors, zero LLM |
| `SPEC-06-manual-curation.md` | 6 | 4.2 | Proposals, diffs, approvals, Agent-C, evidence retrieval, **graph explorer** |
| `SPEC-07-agent-pipeline.md` | 7 | 4.3 | Orchestrator, Agent-A/B/P, pipeline jobs, **agent telemetry** |
| `SPEC-08-hardening.md` | 8 | — | Dry-run, CI, **monitoring polish**, logging hardening, ADRs |

### Step 4: Update CLAUDE.md

Apply these changes to the existing `CLAUDE.md`:

#### 4A — Section 2 (Repository Structure)

Add to the `api/` tree:
```
│   ├── cache/           # Redis-backed cache abstraction layer
│   └── observability/   # Centralized logging, metrics, correlation IDs
```

Add after the `api/` block:
```
├── docs/                # Governance artifacts (specs, skills, ADRs)
│   ├── specs/           # Increment specification documents (SPEC-01 through SPEC-08)
│   ├── skills/          # Cross-cutting skill definitions (SKILL-A, B, C, D)
│   └── adr/             # Architecture Decision Records
```

#### 4B — Section 3 (Tech Stack)

Add rows:
```
| Caching          | Redis (shared with ARQ)             |
| Observability    | structlog + in-memory metrics        |
```

#### 4C — Section 11.3 (Environment Variables)

Add:
```bash
# Observability
LOG_FORMAT                 # json (production) or console (development)
LOG_LEVEL                  # DEBUG, INFO, WARNING, ERROR — default INFO
```

#### 4D — Section 17 (Skills) — REPLACE entire placeholder

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
```

#### 4E — Section 18 (What Claude Should Always Do)

Add:
```markdown
- Before implementing any increment, read the relevant spec in `docs/specs/` and all skills in `docs/skills/`
- After completing any increment, run the SKILL-B governance checklist and update README.md and CLAUDE.md
- Use the centralized logger from `api/observability/logger.py` — never create ad-hoc loggers
- Check the cache before expensive reads — use `api/cache/client.py` with deterministic keys
- Log structured events with correlation IDs — never free-text log messages
```

#### 4F — Section 19 (What Claude Must Never Do)

Add:
```markdown
- Create ad-hoc loggers or use Python's `logging.basicConfig()` directly
- Construct cache keys by string concatenation — use `CacheKey` builders
- Log credentials, API keys, or raw document content
- Perform expensive reads without checking the cache first
- Add business logic to the monitoring UI page
```

---

## Execution Sequence

| Step | Action |
|------|--------|
| 1 | Create `docs/specs/`, `docs/skills/`, `docs/adr/` directories |
| 2 | Place `SKILL-A-api-contracts.md` in `docs/skills/` |
| 3 | Place `SKILL-B-governance.md` in `docs/skills/` |
| 4 | Place `SKILL-C-packaging.md` in `docs/skills/` |
| 5 | Place `SKILL-D-observability.md` in `docs/skills/` |
| 6 | Place `SPEC-01` through `SPEC-08` in `docs/specs/` |
| 7 | Apply CLAUDE.md updates (4A through 4F) |
| 8 | Delete this file from the repo root |

**After Step 8, `CLAUDE.md` is the single entry point.** To implement, tell Claude Code: "Implement SPEC-01" (or 02, 03, etc.).
