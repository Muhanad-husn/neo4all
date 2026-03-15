# Changelog

All notable changes to neo4all are documented in this file.

## [1.0.4] - 2026-03-15

### Added
- Tabbed dashboard with 6 tabs: Overview, Schema, Ingestion, Extraction, Curation, Graph
- Plotly interactive charts replacing basic `st.bar_chart` (gauges, donuts, stacked bars, horizontal bars)
- Schema tab: read-only locked schema display with node/edge type tables and property count donut chart
- Ingestion tab: parser tier distribution chart, document table with parser and source columns, per-document chunk expanders, quality flag distribution donut
- Extraction tab: completion gauge, per-chunk entity stacked bar chart, model info display, auto-refresh every 2s
- Curation tab: candidate archive with summary table and type chart, proposal history with state donut and class bar charts, agent pipeline progress with auto-refresh every 5s and model info, agent telemetry table with token usage chart, exclusions/rejections filter
- Graph tab: Plotly horizontal bar charts for node/edge counts by type (sorted descending)
- Shared `api_fetch.py` helper replacing duplicated `_fetch()` patterns across UI modules
- Shared `plotly_helpers.py` with consistent chart factories (`bar_chart`, `donut_chart`, `gauge_chart`, `stacked_bar`)
- `parser_used` and `source_identity` fields on `DocumentManifest` and `DocumentSummary` models (backward-compatible defaults)
- `extraction_model` property and setter on `StateManager` for tracking LLM model selection
- `plotly>=5.20.0,<6.0.0` dependency

### Changed
- Dashboard page rewritten from 434-line monolith to ~55-line thin tab dispatcher with 6 dedicated tab modules (R-B7)
- `DocumentManifest` now carries `parser_used` and `source_identity` from ingestion through to the list endpoint

---

## [1.0.3] - 2026-03-15

### Added
- Phase re-entry: users can navigate from Curation back to Ingestion to add more documents within the same run, then extract only new chunks and return to Curation
- "Add More Documents" button in the Curation sidebar
- `reenter_phase()` and `clear_reentry()` methods on `StateManager` with allowed-path validation (CURATION→INGESTION, EXTRACTION→INGESTION)
- `reentry_source` field on `SessionRecord` for persistence across browser refresh
- Re-entry info banners on Ingestion and Extraction pages during re-entry mode
- Reconciliation suppression during re-entry to prevent auto-advancement
- Unit tests for phase re-entry transitions, persistence, and round-trip flow (`test_phase_reentry.py`)

---

## [1.0.2] - 2026-03-12

### Added
- Exclude proposal state: permanently suppress noisy candidates from future detection (pending -> excluded, re-openable)
- POST `/proposals/{id}/exclude` and `/proposals/{id}/restore` approval gate endpoints
- Excluded candidate filtering in `GET /candidates/{run_id}` via Redis-backed excluded set
- Bulk "Delete All Orphan Nodes" endpoint (`POST /orphans/delete-all`) — creates, approves, and executes delete proposals through the full governed pipeline
- UI: Exclude button alongside Approve/Reject/Defer in proposal queue
- UI: Delete All Orphan Nodes button in structural anomaly candidate group
- UI: Excluded Items section with per-item and batch Restore functionality
- GET `/proposals/{run_id}/excluded` endpoint for filtered excluded proposals view
- `CacheKey.excluded_candidates(run_id)` cache key builder

---

## [1.0.1] - 2026-03-12

### Added
- Show-all-rows toggle to graph explorer tables
- Enhanced dashboard and pipeline pages with detailed monitoring
- Deterministic chain merge fast-path to Agent-P

### Changed
- Default model changed from gpt-4o-mini to openai/gpt-5-mini

### Fixed
- Evidence endpoint cache miss due to stage-keyed candidates

---

## [1.0.0] - 2026-03-07

### Initial Public Release

neo4all is an AI-powered platform that transforms documents into a governed knowledge graph in Neo4j. Every graph mutation flows through: Proposal, Approval, Diff, Execution, Audit.

### Completed Increments

- **SPEC-01 (v0.1.0)** — Scaffolding, session lifecycle, structured logging, Redis caching, monitoring endpoints
- **SPEC-02 (v0.2.0)** — Domain schema definition with AI-assisted proposal, human editing, and immutable locking
- **SPEC-03 (v0.3.0)** — Document ingestion and chunking with three-tier parser fallback (Docling, Unstructured, raw text) supporting 20+ file formats
- **SPEC-04 (v0.4.0)** — AI-assisted extraction via ARQ worker jobs with per-chunk LLM extraction, validation, and Neo4j writes
- **SPEC-05 (v0.5.0)** — Deterministic candidate generation with five zero-LLM quality detectors (exact/probable duplicates, canonical violations, structural anomalies)
- **SPEC-06 (v0.6.0)** — Manual curation with evidence retrieval, proposal pipeline, two-phase approval for high-risk operations, and immutable audit trail
- **SPEC-07 (v0.7.0)** — AI curation agent pipeline (Orchestrator, Agent-A/B/P) with risk-based budgeting, safety guards, and per-agent telemetry
- **SPEC-08 (v0.8.0)** — Monitoring polish, logging hardening, CI pipeline, dry-run mode, and documentation

### Highlights

- Full governed pipeline: no graph mutation without human approval
- 20+ document formats supported out of the box
- Deterministic IDs for all artifacts (no uuid4)
- Five zero-LLM quality detectors
- Multi-agent curation with regex safety guards against Cypher/executable injection
- Dry-run mode for zero-risk validation
- Docker Compose for local development; AWS ECS Fargate for production
