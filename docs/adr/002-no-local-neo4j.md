# ADR-002: Neo4j Aura Only — No Local Graph Database

**Status**: Accepted
**Date**: 2025-06-01
**Increment**: SPEC-01 (Scaffolding)

---

## Context

The platform uses Neo4j as its source-of-truth graph database. Neo4j can be deployed locally (Community or Enterprise Docker container) or as a managed cloud service (Neo4j Aura).

Local Neo4j containers are common in development workflows, but they introduce several challenges for this project:

- **Schema drift**: Local instances diverge from production configuration (plugins, memory settings, APOC availability) leading to "works on my machine" failures.
- **Data governance**: The proposal-approval-execution pipeline relies on Aura-specific features and constraints. A local instance would need separate validation.
- **Operational burden**: Maintaining Docker volumes, backup scripts, and version-pinned Neo4j containers adds infrastructure complexity without production benefit.
- **Team consistency**: All developers and CI must target the same graph topology and constraints. A shared Aura instance eliminates local divergence.

## Decision

The application targets **Neo4j Aura exclusively** — no local Neo4j container is included in `docker-compose.yml` or supported in the development workflow.

Rules:
- The application **refuses to start** without valid `NEO4J_DEV_URI`, `NEO4J_DEV_USER`, and `NEO4J_DEV_PASSWORD` environment variables (fail-closed per CLAUDE.md 4.4).
- CI uses a separate Aura instance via `NEO4J_CI_URI`, `NEO4J_CI_USER`, `NEO4J_CI_PASSWORD`.
- Integration tests in `tests/integration/` are **skipped** when CI Aura credentials are absent — they are not blocking for local development.
- Health probes at startup log a WARNING on Neo4j connection failure but do not `sys.exit` — connectivity issues are transient and distinct from missing credentials.
- All graph reads go through `api/graph/reader.py` (cache-first) and all writes go through the proposal pipeline terminating in Agent-C (`api/agents/execution.py`).

## Consequences

### Positive

- **Environment parity**: Dev, CI, and production all target Aura instances with identical capabilities (constraints, indexes, APOC). No behavioral surprises at deployment.
- **Simplified infrastructure**: `docker-compose.yml` runs only FastAPI, Streamlit, Redis, RustFS, and Qdrant. No Neo4j container to configure, version-pin, or troubleshoot.
- **Governance integrity**: The approval pipeline and Agent-C's post-apply invariant checks behave identically across all environments because the graph engine is the same.
- **Reduced Docker image size**: No Neo4j volume mounts or plugin management in the development stack.

### Negative

- **No offline development**: Developers require network access to an Aura instance for any graph-related work. This rules out airplane/offline development for graph features.
- **Credential management**: Every developer needs Aura credentials in their environment. Credential rotation must be coordinated.
- **Cost**: Aura instances incur cloud costs even for development. The free tier is limited and may not support all required features.
- **Integration test gating**: Integration tests only run when Aura credentials are available. Local-only contributors cannot run the full test suite without provisioning access.
- **Startup dependency**: The application cannot demonstrate graph features without a live Aura connection, complicating demos and onboarding.

## References

- [CLAUDE.md §9](../../CLAUDE.md) — Environment variables and Neo4j configuration
- [CLAUDE.md §4.4](../../CLAUDE.md) — Fail-closed behavior on missing credentials
- [CLAUDE.md §10](../../CLAUDE.md) — Integration test gating strategy
- `api/services/health.py` — Neo4j probe implementation
- `api/graph/reader.py` — Read-only graph access (cache-first)
- `api/agents/execution.py` — Agent-C (sole write authority)
