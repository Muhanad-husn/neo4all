# Changelog

All notable changes to neo4all are documented in this file.

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
