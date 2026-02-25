# ADR-003: Deterministic Content-Derived IDs

**Status**: Accepted
**Date**: 2025-06-01
**Increment**: SPEC-01 (Scaffolding), enforced across all increments

---

## Context

The platform manages a pipeline of governed artifacts: documents, chunks, nodes, relationships, candidates, proposals, diffs, and audit records. Each artifact requires a stable identifier for deduplication, caching, idempotent reruns, and audit trail integrity.

The standard approach — `uuid4()` — generates random identifiers that are unique but carry no semantic meaning. This creates several problems:

- **Non-idempotent reruns**: Re-ingesting the same document produces new IDs, duplicating graph entries and breaking manifest-based incremental processing.
- **Non-deterministic tests**: Test assertions cannot predict IDs, requiring fragile pattern matching or post-hoc ID extraction.
- **Deduplication gaps**: Duplicate detection requires secondary indexes on content fields rather than relying on ID collision.
- **Audit ambiguity**: The same logical artifact gets different IDs across runs, complicating audit trail correlation.

## Decision

**All governed artifact IDs are derived deterministically from their content using SHA-256 hashes of stable inputs.** `uuid4()` is banned for any artifact that participates in the governance pipeline.

### ID Derivation Table

| Artifact | Formula | Inputs |
|----------|---------|--------|
| `doc_id` | `SHA-256(run_id + NUL + source_identity + NUL + content_hash)` | Run context, file identity, content fingerprint |
| `chunk_id` | `SHA-256(doc_id + NUL + start_page + NUL + chunk_index)` | Parent document, position within document |
| `node_dedupe_key` | `(NodeType, primary_property, schema_version)` | Graph type system coordinates |
| `rel_dedupe_key` | `(RelType, start_key, end_key, schema_version)` | Relationship endpoints and type |
| `candidate_id` | `SHA-256(candidate_type + NUL + sorted_dedupe_keys + NUL + schema_version)` | Detection type, target elements, schema |
| `proposal_id` | `(run_id, candidate_id, proposal_class)` | Run context, target candidate, action type |
| `diff_id` | `SHA-256(diff_content)` | Deterministic hash of the diff payload |
| `approval_id` | `SHA-256("approval:" + proposal_id + ":" + actor)` | Proposal identity, approving actor |
| `confirmation_token` | `SHA-256("confirm:" + proposal_id + ":" + actor)` | Two-phase confirmation for high-risk actions |

Rules:
- **Null-byte separators** (`NUL`, `\x00`) are used between input components to prevent boundary collisions (e.g., `run_id="ab"` + `source="cd"` vs `run_id="abc"` + `source="d"`).
- `doc_id` and `chunk_id` are `@computed_field` properties on their Pydantic models — they are never accepted as constructor input, only derived.
- Qdrant point IDs use `uuid.UUID(chunk_id[:32])` — a deterministic UUID derived from the SHA-256 prefix, not `uuid4()`.
- All artifacts carry `run_id` and `schema_version` for traceability.
- Tests in `tests/unit/` verify determinism (same input produces same ID), sensitivity (changing any input changes the ID), and collision resistance (boundary cases with null-byte separators).

## Consequences

### Positive

- **Idempotent reruns**: Re-ingesting the same document with the same run context produces identical IDs. The manifest cache can skip parsing when `content_hash` and `parser_config_hash` match, enabling efficient incremental processing.
- **Natural deduplication**: Duplicate artifacts collide on ID, making deduplication a simple existence check rather than a content comparison.
- **Deterministic tests**: All 24 candidate ID tests, 5 doc ID tests, and 6 chunk ID tests assert exact expected values. No randomness means no flaky tests and no pattern matching.
- **Audit integrity**: The same logical artifact always has the same ID regardless of when or how many times it is processed. Audit trails are trivially joinable across runs.
- **Cache alignment**: Cache keys incorporate deterministic IDs (e.g., `CacheKey.manifest(run_id, doc_id)`), so cache hits are reliable and predictable.

### Negative

- **Input stability requirement**: If the derivation inputs change (e.g., a different chunking algorithm changes `chunk_index` ordering), all downstream IDs change. This is by design — changed inputs should produce new IDs — but requires careful migration planning.
- **Hash computation cost**: SHA-256 is computed for every artifact. In practice this is negligible compared to parsing, embedding, and LLM costs.
- **Developer discipline**: Contributors must understand and follow the derivation rules. Using `uuid4()` is a natural habit that must be actively avoided. CI linting and code review enforce this constraint.

## References

- [CLAUDE.md §5](../../CLAUDE.md) — Identity conventions and derivation formulas
- [CLAUDE.md §6](../../CLAUDE.md) — Proposal Packet structure
- [CLAUDE.md §4.3](../../CLAUDE.md) — Determinism requirements
- `api/models/document.py` — `doc_id` and `chunk_id` as `@computed_field`
- `api/models/candidate.py` — `candidate_id` derivation
- `tests/unit/test_doc_id.py` — Document ID determinism tests (5 tests)
- `tests/unit/test_chunk_id.py` — Chunk ID determinism tests (6 tests)
- `tests/unit/test_candidate_id_determinism.py` — Candidate ID tests (24 tests)
