# neo4all User Manual

A complete walkthrough for using neo4all — the AI-powered knowledge graph extraction and curation platform.

---

## Table of Contents

1. [What Is neo4all?](#1-what-is-neo4all)
2. [How It Works (The Big Picture)](#2-how-it-works-the-big-picture)
3. [Getting Started](#3-getting-started)
4. [Phase 0 — Session Initialization](#4-phase-0--session-initialization)
5. [Phase 1 — Domain Schema Definition](#5-phase-1--domain-schema-definition)
6. [Phase 2 — Document Ingestion & Chunking](#6-phase-2--document-ingestion--chunking)
7. [Phase 3 — AI-Assisted Extraction](#7-phase-3--ai-assisted-extraction)
8. [Phase 4 — Curation](#8-phase-4--curation)
9. [Graph Explorer](#9-graph-explorer)
10. [Dashboard](#10-dashboard)
11. [Sidebar & Navigation](#11-sidebar--navigation)
12. [Configuration Reference](#12-configuration-reference)
13. [Key Concepts Glossary](#13-key-concepts-glossary)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. What Is neo4all?

neo4all turns your documents — PDFs, Word files, spreadsheets, emails, images, and more — into a structured **knowledge graph** stored in Neo4j. A knowledge graph represents information as **nodes** (things like people, companies, events) connected by **edges** (relationships like "works at", "reported by", "located in").

The key principle: **AI proposes, you decide.** The AI reads your documents, suggests what entities and relationships exist, and drafts changes to the graph. But nothing is written to the database until you review and approve it. Every change goes through a governed pipeline:

```
AI Proposal → Human Approval → Deterministic Diff → Execution → Audit Log
```

This means the AI can never silently alter your data. You are always in control.

---

## 2. How It Works (The Big Picture)

neo4all operates in five sequential phases. Each phase builds on the previous one:

| Phase | What Happens | Who Does The Work |
|-------|-------------|-------------------|
| **0. Session Init** | Connect to your Neo4j database and LLM provider | You |
| **1. Schema Definition** | Define what types of nodes and edges your graph will contain | AI proposes, you edit and approve |
| **2. Document Ingestion** | Upload documents; the system parses and chunks them | Automated parsers |
| **3. Extraction** | AI reads each chunk and creates nodes/edges in the graph | AI (LLM), batched via background workers |
| **4. Curation** | Detect duplicates, violations, and anomalies; then fix them | Automated detection + AI proposals + your approval |

You can freely navigate between phases using the sidebar — the system trusts you to know when prerequisites are ready.

### The Technology Stack in Plain English

- **Streamlit** (the web interface): What you see in your browser. Buttons, tables, file uploads — all rendered by Streamlit.
- **FastAPI** (the backend): Handles all the logic. When you click a button, the UI sends a request to the backend API.
- **Redis** (the queue and cache): Stores your session, caches expensive queries, and queues background jobs (extraction, agent pipeline).
- **Neo4j Aura** (the graph database): Where your knowledge graph lives. The only source of truth.
- **Qdrant** (the vector store): Stores document chunks as numerical embeddings so the system can find relevant evidence by meaning, not just keywords.
- **RustFS/S3** (object storage): Stores audit logs, document manifests, and proposal artifacts.
- **OpenRouter** (LLM gateway): Routes AI requests to language models. You provide an API key, and the system uses it for schema proposals, extraction, and curation agents.

---

## 3. Getting Started

### Prerequisites

- A **Neo4j Aura** instance (free tier works). Sign up at [neo4j.com/aura](https://neo4j.com/cloud/platform/aura-graph-database/) and note your URI, username, and password.
- An **OpenRouter API key**. Sign up at [openrouter.ai](https://openrouter.ai/) and create a key.
- **Docker** and **Docker Compose** installed on your machine.

### Launch the Application

```bash
# Clone the repository
git clone https://github.com/your-org/neo4all.git
cd neo4all

# Create your environment file
cp .env.example .env
# Edit .env and fill in your Neo4j and OpenRouter credentials

# Start all services
docker compose up
```

This brings up five containers:

| Service | URL | Purpose |
|---------|-----|---------|
| **UI** | http://localhost:8501 | The web app you interact with |
| **API** | http://localhost:8000 | Backend (also serves interactive API docs at `/docs`) |
| **Redis** | localhost:6379 | Cache and job queue |
| **Qdrant** | localhost:6333 | Vector search engine |
| **RustFS** | localhost:9000 | Local S3-compatible object storage |

Open **http://localhost:8501** in your browser to begin.

### Updating Code (Without Rebuilding)

If you make code changes to Python files, you don't need to rebuild Docker images:

```bash
./hotpatch.sh
```

This copies updated files into running containers and restarts them in seconds. Only rebuild (`docker compose up -d --build`) when you change dependencies in `pyproject.toml` or modify Dockerfiles.

---

## 4. Phase 0 — Session Initialization

**What you see:** A connection form with four fields.

**What to do:**

1. **Neo4j URI** — Paste your Aura connection string (e.g., `neo4j+s://abc123.databases.neo4j.io`).
2. **Neo4j Username** — Usually `neo4j`.
3. **Neo4j Password** — Your Aura password.
4. **OpenRouter API Key** — Your `sk-or-v1-...` key.

Click **Initialize Session**.

**What happens behind the scenes:**

- The system derives a deterministic **run ID** from your Neo4j credentials. This run ID scopes everything — documents, chunks, nodes, edges, proposals — so different users or databases never collide.
- Credentials are saved to a `.env` file so the backend API can use them.
- Your session state is persisted to Redis. If you close the browser and reopen it, the app will restore your session automatically (same credentials, same phase, same progress).

**Tips:**
- If you've used the app before with the same Neo4j instance, your previous session will be auto-restored. You'll land on whichever phase you were last on.
- Click **New Session** in the sidebar at any time to start fresh (this clears session state but does NOT delete data from Neo4j).

---

## 5. Phase 1 — Domain Schema Definition

**Purpose:** Tell the system what kinds of things exist in your domain before it starts reading documents.

Think of the schema as a blueprint. If you're building a graph about a company, your schema might define node types like `Person`, `Company`, `Product` and edge types like `WORKS_AT`, `MANUFACTURES`, `REPORTS_TO`.

### Two Ways to Define a Schema

**Option A: AI Generate (recommended for first-time users)**

1. Select the **AI Generate** tab.
2. Write a plain-English description of your domain. Be specific about the types of entities and relationships you care about. For example:

   > "Academic research papers with authors, institutions, journals, citations, and research topics. Authors write papers, papers cite other papers, authors are affiliated with institutions, papers are published in journals, and papers cover research topics."

   Minimum 10 words.

3. Choose an **LLM Model** (the default works well; you can change it if you have a preferred model on OpenRouter).
4. Click **Propose Schema**.
5. Wait 10–30 seconds for the AI to generate a proposal.

**Option B: Upload Schema**

1. Select the **Upload Schema** tab.
2. Paste a JSON schema definition following the format shown in the expandable reference.
3. Click **Upload & Validate**.

### Editing the Proposal

After the AI proposes (or you upload), you'll see two editable tables:

**Node Types Table:**

| Column | Meaning | Example |
|--------|---------|---------|
| `node_class` | Category (entity, event, concept, etc.) | `entity` |
| `type` | The Neo4j label | `Person` |
| `primary_property` | The main identifying field (used for deduplication) | `name` |
| `qualifier` | Optional secondary disambiguator | `birth_year` |
| `additional_properties` | Comma-separated extra fields | `email, title, department` |

**Edge Types Table:**

| Column | Meaning | Example |
|--------|---------|---------|
| `start_node_type` | Source node type | `Person` |
| `end_node_type` | Target node type | `Company` |
| `type` | Relationship label (SCREAMING_SNAKE_CASE) | `WORKS_AT` |
| `primary_property` | Main property on the relationship | `role` |
| `qualifier` | Optional disambiguator | `start_date` |
| `additional_properties` | Extra fields | `department, location` |

You can:
- **Edit any cell** by clicking on it.
- **Add rows** using the "+" button at the bottom of each table.
- **Delete rows** by selecting the checkbox and pressing Delete.
- **Re-propose** if you want the AI to try again (clears the current proposal).

### Approving the Schema

When you're satisfied, click **Approve & Lock Schema**.

This is a one-way action for this run:
- The schema becomes **immutable** — extraction and curation will use exactly this schema.
- A **version hash** is generated and stamped on every artifact created from this point on.
- The schema is cached in Redis for the lifetime of the run.

After approval, click **Proceed to Phase 2: Document Ingestion**.

**Tips:**
- Be thoughtful here. The schema shapes everything downstream. A missing node type means the AI won't extract those entities. An extra one adds noise.
- You can always start a new session to try a different schema.
- The schema *can* be amended during curation (Phase 4) if the AI discovers edge types that are valid but weren't in the original schema.

---

## 6. Phase 2 — Document Ingestion & Chunking

**Purpose:** Upload your source documents and let the system parse them into structured chunks.

### Uploading Documents

1. Click the file uploader or drag-and-drop a file.
2. Supported formats: **PDF, DOCX, PPTX, XLSX, CSV, TSV, HTML, TXT, MD, RST, RTF, ODT, MSG, EML, EPUB, PNG, JPG, TIFF, BMP**.
3. Click **Ingest Document**.

You can upload multiple documents one at a time. Each upload triggers the full ingestion pipeline.

### What Happens During Ingestion

The system uses a **three-tier parser fallback** strategy:

1. **Docling** (first choice) — A structural parser that understands tables, headings, captions, and document layout. Produces the richest metadata.
2. **Unstructured** (fallback) — A broad-format parser. Handles more file types but with less structural awareness.
3. **Raw Text** (last resort) — Extracts plain text using basic tools (PyPDF2, python-docx, or UTF-8 decode). Works on almost anything but produces no structural metadata.

If Tier 1 fails, the system automatically falls back to Tier 2, then Tier 3. Each fallback is logged. You can disable tiers via environment variables if needed (see [Configuration Reference](#12-configuration-reference)).

After parsing, the document is split into **chunks** — contiguous segments of text that are semantically coherent:
- Tables become standalone chunks (one table = one chunk).
- Headings act as chunk boundaries.
- Paragraphs accumulate until a size threshold is reached.

Each chunk gets a deterministic ID, quality flags, and is embedded as a vector in Qdrant for later semantic search.

### The Ingestion View

After uploading, you'll see:

- **Document List** — A table of all ingested documents showing doc ID, chunk count, and ingestion timestamp.
- **Chunk Manifest** — Expandable per-document sections showing each chunk's metadata: ID, page range, character count, and quality flags.
- **Activity Feed** — A live-updating log of ingestion events (auto-refreshes every 5 seconds).

**Quality Flags (color-coded):**

| Flag | Color | Meaning |
|------|-------|---------|
| `raw_fallback` | Red | Parsed by the raw-text tier; no structural metadata available |
| `low_ocr_confidence` | Yellow | OCR was used but confidence is low (scanned/image documents) |
| `low_text_density` | Yellow | Very little text relative to document size |

These flags don't block processing — they're informational so you know which documents may produce lower-quality extraction results.

### Incremental Ingestion

Ingestion is **idempotent**. If you re-ingest the same document:
- The system computes a content hash and checks the cached manifest.
- If both the content and parser configuration are unchanged, parsing is skipped entirely.
- Only genuinely new or modified documents are processed.

When you have at least one document ingested, click **Proceed to Phase 3: AI-Assisted Extraction**.

---

## 7. Phase 3 — AI-Assisted Extraction

**Purpose:** The AI reads each document chunk and populates the knowledge graph with nodes and edges according to your schema.

### Starting Extraction

1. Choose an **LLM Model** (the default is pre-filled; you can change it).
2. Click **Start Extraction**.

The system enqueues one background job per batch of chunks (controlled by `EXTRACTION_BATCH_SIZE`, default 5). Jobs run in parallel via ARQ workers.

### Monitoring Progress

The page auto-refreshes every 2 seconds while extraction is running. You'll see:

- **Metrics row**: Total chunks, Completed, Failed, Pending.
- **Progress bar**: Visual percentage with count (e.g., "75% — 15/20 resolved").
- **Entity yield**: Running totals of nodes and edges extracted so far.
- **Failed chunks** (if any): Expandable section showing which chunks failed and why.

You can click **Stop Extraction** at any time to cancel remaining jobs (already-completed chunks are preserved).

### What the AI Does Per Chunk

For each batch of chunks, the AI:

1. Receives the chunk text plus your locked schema as context.
2. Identifies entities (nodes) mentioned in the text and maps them to your schema's node types.
3. Identifies relationships (edges) between those entities.
4. Returns structured output that the system validates against your schema.
5. Valid nodes and edges are written to Neo4j with full provenance (which chunk they came from, which run, which schema version).

The extraction prompt is versioned and stored in `/prompts/` — it's not generated on the fly.

### After Extraction Completes

You'll see a summary:
- Total nodes and relationships created.
- Per-chunk breakdown table (how many entities each chunk contributed).
- Any failed chunks with details.

**If everything succeeded:** Click **Proceed to Curation**.

**If some chunks failed:** You have two choices:
- **Re-run Extraction** — Retries only the failed/pending chunks (completed chunks are skipped).
- **Proceed with partial results** — Move on to curation with whatever was successfully extracted.

**Tips:**
- Extraction is idempotent. Re-running won't duplicate already-processed chunks.
- Larger `EXTRACTION_BATCH_SIZE` means fewer API calls but higher token usage per call. The default of 5 balances cost and reliability.
- Check the Dashboard (Tab 3: Extraction) for detailed timing and per-type entity breakdowns.

---

## 8. Phase 4 — Curation

**Purpose:** Clean up and refine your knowledge graph. This is where the real quality work happens.

Curation has three layers, each progressively more sophisticated:

### Layer 1: Candidate Detection (Automated, No AI)

The system scans your graph for potential issues using five deterministic detectors. No LLM is involved — this is pure pattern matching and statistics.

**Stage 1 — Duplicates:**

| Detector | What It Finds | Example |
|----------|--------------|---------|
| **Exact Node Duplicate** | Two nodes with identical type + primary property | Two `Person` nodes both named "Jane Smith" |
| **Exact Relationship Duplicate** | Two edges with same type, start, and end nodes | Two `WORKS_AT` edges between the same person and company |
| **Probable Duplicate** | Nodes with very similar (but not identical) primary properties | "Jonathan Jones" vs "Jonathon Jones" (Jaro-Winkler similarity > 0.90) |

**Stage 2 — Canonical Violations:**

| Detector | What It Finds | Example |
|----------|--------------|---------|
| **Canonical Violation** | Edges that violate the schema's type constraints or direction | A `WORKS_AT` edge going from Company to Person (backwards) |

**Stage 3 — Structural Anomalies:**

| Detector | What It Finds | Example |
|----------|--------------|---------|
| **Hub Node** | Nodes with abnormally many connections (statistical outlier at 3 standard deviations) | A `Person` node connected to 50 other nodes when the average is 3 |
| **Orphan Node** | Nodes with zero connections | A `Company` node that nothing points to or from |
| **Missing Provenance** | Nodes or edges lacking required tracking metadata | A node without `run_id` or `schema_version` |

**Using the Candidate Review:**

- Click **Generate All Stages** to run all detectors at once, or run them individually by stage.
- Results appear as expandable groups organized by detector type.
- Each candidate shows a **severity** (CRITICAL, HIGH, MEDIUM, LOW) and a human-readable summary.
- Candidates are cached for 5 minutes (to avoid re-scanning on every page load).
- Click **Clear All Candidates** to reset and re-detect from scratch.

**Special Actions:**
- For canonical violations, a **Schema Amendment** button lets you accept the violation as valid (adds the edge type to the schema).
- For orphan nodes, a **Delete All Orphans** button offers a bulk cleanup.

### Layer 2: Manual Proposals & Approval Queue

For any candidate, you can manually create a proposal to fix it:

1. **Select a candidate** from the dropdown.
2. **View evidence** — the system retrieves relevant document chunks from Qdrant to help you understand the context.
3. **Choose a proposal class:**

   | Class | What It Does |
   |-------|-------------|
   | `canonicalize` | Standardize a property value to canonical form |
   | `normalize` | Normalize formatting (capitalization, whitespace, etc.) |
   | `rename` | Change a node's primary property value |
   | `merge` | Combine two duplicate nodes into one (high-risk) |
   | `delete` | Remove a node or edge (high-risk) |
   | `defer` | Flag for later review; no graph change |

4. **Write a rationale** explaining your decision.
5. Click **Submit Proposal**.

**Approval Queue:**

All proposals (manual and AI-generated) appear in the approval queue:

- **Low-risk proposals** (canonicalize, normalize, rename, defer): Single-click **Approve**, then **Execute**.
- **High-risk proposals** (merge, delete): Two-phase approval — click **Approve**, then **Confirm** in a separate step. This prevents accidental destructive changes.
- **Reject**: Dismiss a proposal permanently.
- **Defer**: Set aside for later.

**Executing a proposal:**
1. An approved proposal generates a **deterministic diff** — a precise set of graph operations (create, update, delete) that will be applied.
2. You enter the approval ID and click **Execute**.
3. Agent-C (the execution agent) validates the approval, applies the diff, runs post-apply invariant checks, invalidates caches, and writes an immutable audit record.

### Layer 3: AI Agent Pipeline

For large graphs with many candidates, manual curation doesn't scale. The AI agent pipeline automates the analysis and proposal process.

**The agents:**

| Agent | Role | Type |
|-------|------|------|
| **Orchestrator** | Routes candidates, assigns risk levels and token budgets | Non-AI (deterministic) |
| **Agent-A** (Evidence Assembly) | Reads graph context and document chunks, classifies the evidence for/against each candidate | AI (LLM) |
| **Structural Recommender** | Pre-digests collision patterns and suggests likely actions | Non-AI (advisory only) |
| **Agent-B** (Retrieval Augmentation) | Searches for additional evidence when Agent-A says "not enough info" | AI (LLM), loop-guarded |
| **Agent-P** (Proposal Composer) | Writes a formal Proposal Packet based on all gathered evidence | AI (LLM) |
| **Diff Builder** | Translates the proposal into a deterministic graph diff | Non-AI |

**Running the pipeline:**

1. Choose LLM models for each agent (defaults are pre-filled).
2. Optionally enable **Agent-B** (retrieval augmentation) via the checkbox. Agent-B is off by default because it adds cost and latency; enable it when Agent-A frequently reports insufficient evidence.
3. Click **Run Agent Pipeline**.
4. Monitor progress as agents process candidates in batches (default batch size: 8).

**Batch processing:** Agents A and P process multiple candidates in a single LLM call, sharing system prompt context. This reduces API round-trips and cost.

**Safety guardrails:**
- Agents can never generate or execute Cypher queries.
- Agent-P's output is regex-scanned for Cypher patterns or executable code — if found, the proposal is blocked.
- Agent-B has a loop guard (`max_retrieval_rounds`, default 3) to prevent runaway searches.
- Each candidate has a cost limit (default $0.10) across all agents.

**After the pipeline completes**, all AI-generated proposals enter the same approval queue as manual proposals. You still review and approve each one.

**Tips:**
- Start with Layer 1 to understand the scope of issues in your graph.
- Use Layer 2 for targeted manual fixes on critical items.
- Use Layer 3 when you have dozens or hundreds of candidates to process.
- You can mix and match: run the AI pipeline for bulk analysis, then manually approve/reject each proposal.

---

## 9. Graph Explorer

Available at any time via the sidebar **Graph Explorer** button.

A read-only, paginated browser for your knowledge graph:

**Nodes Tab:**
- See total node count and filter by node type.
- Table columns: Node type, Dedupe key, Primary value, Schema version, Properties preview.
- Expand any row to see full properties.
- Default 50 per page; toggle "Show all" for up to 5,000.

**Edges Tab:**
- See total edge count and filter by edge type.
- Table columns: Relationship type, Start node, End node, Schema version, Properties preview.
- Expand any row to see full properties including start/end dedupe keys.

The Graph Explorer is **always available** — even before extraction, it will show an empty graph. Use it at any phase to verify what's in your database.

---

## 10. Dashboard

Available at any time via the sidebar **Dashboard** button.

A read-only monitoring view with six tabs:

| Tab | Shows |
|-----|-------|
| **Overview** | Document count, entity count, job count, proposal count, worker status, cache stats, recent activity log |
| **Schema** | Read-only view of your locked schema (node types, edge types, amendments) |
| **Ingestion** | Documents ingested, parser tier distribution, chunk quality breakdown, average chunk size |
| **Extraction** | Progress percentages, per-type entity breakdown (bar charts), timing, model used |
| **Curation** | Candidates by stage, proposal status breakdown, agent pipeline metrics |
| **Graph** | Node/edge count trends (time series chart), top 10 node and edge types (bar charts) |

The Dashboard never modifies anything — it's purely observational. Use it to understand the overall health and progress of your run.

---

## 11. Sidebar & Navigation

The sidebar is always visible and contains:

**Session Controls:**
- **New Session** button — Clears your session and returns to Phase 0 (does NOT delete Neo4j data).

**Phase Navigation:**
- Buttons for each phase with status markers:
  - **checkmark** = phase completed
  - **arrow** = current watermark phase
  - **circle** = not yet reached
- Click any phase button to navigate there. The system won't block you but will show informational messages if prerequisites aren't met.

**Utility Views:**
- **Dashboard** — Monitoring and analytics.
- **Graph Explorer** — Browse your graph.

**Run Info** (when a session is active):
- Run ID (first 16 characters)
- Neo4j URI
- Username
- Schema version (after approval)
- Session elapsed time

**Activity Feed:**
- Expandable section in the sidebar.
- Shows the 8 most recent log entries with severity dots and relative timestamps.
- Auto-refreshes every 15 seconds.

---

## 12. Configuration Reference

### Required Environment Variables

Set these in your `.env` file before starting:

| Variable | Description |
|----------|-------------|
| `NEO4J_DEV_URI` | Neo4j Aura connection URI (e.g., `neo4j+s://xxx.databases.neo4j.io`) |
| `NEO4J_DEV_USER` | Neo4j username (usually `neo4j`) |
| `NEO4J_DEV_PASSWORD` | Neo4j password |
| `OPENROUTER_API_KEY` | Your OpenRouter API key |
| `S3_ENDPOINT_URL` | Object storage endpoint (`http://localhost:9000` for local) |
| `S3_ACCESS_KEY_ID` | S3 access key (`minioadmin` for local RustFS) |
| `S3_SECRET_ACCESS_KEY` | S3 secret key (`minioadmin` for local RustFS) |
| `S3_BUCKET_NAME` | S3 bucket name (e.g., `neo4all`) |
| `REDIS_URL` | Redis connection string (`redis://localhost:6379`) |

### Optional Tuning Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_FORMAT` | `json` | Log format: `json` (production) or `console` (development) |
| `LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `EXTRACTION_BATCH_SIZE` | `5` | Chunks per LLM call during extraction (1–50). Higher = fewer API calls, more tokens per call |
| `DRY_RUN` | `false` | When `true`, the pipeline runs fully but skips all Neo4j writes. Diffs are logged to S3 for review |
| `EXECUTION_COOLDOWN_SECONDS` | `10` | Seconds to wait after a graph mutation before allowing candidate re-detection |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model for chunk embeddings |

### Parser Toggles

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_DOCLING` | `true` | Enable the Docling structural parser (best quality) |
| `ENABLE_UNSTRUCTURED` | `true` | Enable the Unstructured parser (broad format support) |
| `ENABLE_RAW_FALLBACK` | `true` | Enable raw-text extraction (last resort) |

### Agent Pipeline Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_AGENT_B` | `false` | Server default for retrieval augmentation (UI checkbox overrides per run) |
| Batch size | `8` | Candidates per LLM call in batch mode (1–20) |
| Cost limit | `$0.10` | Maximum USD per candidate across all agents |
| Max retrieval rounds | `3` | Agent-B loop guard |
| Reranking top N | `5` | Chunks returned by reranking for agent context |

Agent LLM models (Agent-A, B, P) are selectable in the UI at runtime. Defaults are configured in `api/config.py`.

### Dry-Run Mode

Set `DRY_RUN=true` to run the entire pipeline — ingestion, extraction, candidate detection, agent analysis — without writing anything to Neo4j. All diffs that *would have* been applied are logged to S3 for inspection. This is ideal for:
- Validating your schema and pipeline before committing to a graph.
- Testing new document types or models.
- Auditing what the AI would do without risk.

---

## 13. Key Concepts Glossary

| Term | Meaning |
|------|---------|
| **Run** | A complete session scoped to a specific Neo4j instance and user. All artifacts (documents, chunks, nodes, proposals) belong to a run |
| **Run ID** | A deterministic identifier derived from your Neo4j credentials. Same credentials = same run ID = same session |
| **Schema** | The blueprint defining what node types and edge types your graph can contain. Locked after approval |
| **Schema Version** | A hash of the approved schema. Stamped on every node, edge, and proposal for traceability |
| **Chunk** | A semantically coherent segment of a document. The basic unit the AI reads during extraction |
| **Node** | An entity in the graph (e.g., a person, company, event). Has a type, primary property, and optional qualifiers |
| **Edge** | A relationship between two nodes (e.g., WORKS_AT, CITES). Has a type, start node, and end node |
| **Dedupe Key** | A composite key (type + primary property + schema version) that uniquely identifies a node or edge. Used to detect duplicates |
| **Candidate** | A potential issue detected in the graph (duplicate, violation, anomaly) that may need curation |
| **Proposal** | A formal request to change the graph, with a class (merge, delete, rename, etc.), evidence, and rationale |
| **Proposal Packet** | The full structure an AI agent produces: linkage, intent, evidence references, governance metadata, and targets |
| **Diff** | A deterministic set of graph operations (CREATE, UPDATE, DELETE) generated from an approved proposal |
| **Approval Gate** | The human checkpoint. No graph mutation can happen without an explicit approval (and confirmation for high-risk changes) |
| **Audit Record** | An immutable log entry created after every execution, recording what changed, who approved it, and the outcome |
| **Provenance** | The trail of evidence linking a graph element back to the document chunk and run that created it |

---

## 14. Troubleshooting

### "Session could not be restored"

Your Redis cache may have been cleared (e.g., Docker restart). Simply re-enter your credentials in Phase 0 to start a new session. Your Neo4j data is safe — it lives in Aura, not locally.

### Extraction jobs stuck at "Pending"

Check that the ARQ worker container is running:
```bash
docker compose ps
```
If the worker is down, restart it:
```bash
docker compose restart worker
```

### Parser fallback warnings

Seeing "raw_fallback" quality flags on chunks? This means the Docling and Unstructured parsers couldn't handle the document. The raw text tier extracted what it could, but without structural metadata. Consider:
- Converting the document to a better-supported format (PDF or DOCX).
- Checking that parser tiers are enabled (`ENABLE_DOCLING=true`, `ENABLE_UNSTRUCTURED=true`).

### High-risk approval requires two clicks

This is by design. Merge and delete operations are destructive, so the system requires a two-phase approval: first **Approve** (which generates a confirmation token), then **Confirm** (which issues the final approval ID). This prevents accidental data loss.

### Agent pipeline cost concerns

Each candidate has a configurable cost limit (default $0.10). To reduce costs:
- Decrease `batch_size` if individual calls are too expensive.
- Disable Agent-B (retrieval augmentation) unless needed.
- Use a cheaper model for Agent-A (evidence assembly), which handles the highest volume.
- Use `DRY_RUN=true` to test the pipeline without graph mutations.

### "Schema version mismatch"

This means a proposal or diff was created against a different schema version than what's currently locked. This is a safety check — the system refuses to apply changes based on stale schema. Re-generate candidates and proposals to use the current schema.

### Cannot connect to Neo4j

- Verify your Aura instance is running (check the Aura console).
- Confirm the URI starts with `neo4j+s://` (TLS required for Aura).
- Check that your password is correct (Aura passwords are case-sensitive).
- The app will log a WARNING at startup if Neo4j is unreachable but will not crash — other features still work.
