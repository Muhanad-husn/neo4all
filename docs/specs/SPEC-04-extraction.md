# SPEC-04: AI-Assisted Extraction (Phase 3)

**Increment**: 4 | **Version target**: 0.4.0 | **Prerequisites**: SPEC-03 complete
**Skills required**: SKILL-A, SKILL-B, SKILL-D

---

## Objective

Implement Phase 3: chunks sent to LLM with locked schema, extracted nodes/edges validated and written to Neo4j. This is the one phase where writes go directly to the graph (no proposal pipeline). First use of the ARQ worker.

---

## Specifications

### S-04.1: ARQ Worker
Create `api/worker/entry.py`: ARQ entry point, Redis connection, job imports.
Create `api/worker/jobs.py`: `extraction_job(run_id, chunk_id)` — processes one chunk.

### S-04.2: Extraction Service
Create `api/services/extraction.py`: sends chunk text + schema to OpenRouter via `prompts/extraction/v1.yaml`, validates against `ExtractionResult`, fail-closed on malformed output. Cache locked schema via `CacheKey.schema(run_id)`.

### S-04.3: Extraction Models
Create `api/models/extraction.py`: `ExtractedNode`, `ExtractedEdge`, `ExtractionResult` with schema_version linkage.

### S-04.4: Graph Writer
Create `api/graph/client.py`: Neo4j driver wrapper — pooling, sessions, retry.
Create `api/graph/writer.py`: parameterized Cypher only (CLAUDE.md Section 4.2). MERGE on dedupe keys.

### S-04.5: Endpoints
Create `api/routers/extraction.py`: `POST /api/extraction/run`, `GET /api/extraction/status/{run_id}`, `GET /api/extraction/results/{run_id}`.

### S-04.6: Prompt Template
Create `prompts/extraction/v1.yaml`: chunk text + schema placeholders, output contract.

### S-04.7: UI
Create `ui/pages/extraction.py`: Phase 3 — trigger, per-chunk progress, summary, entity preview.

### S-04.8: Worker Monitoring
Update `api/routers/monitoring.py`: `GET /api/monitoring/workers` (queue depth, active jobs), `GET /api/monitoring/jobs/{run_id}` (per-job status).
Update `ui/pages/monitoring.py`: worker status panel, per-run job tracker.

---

## Files to Generate

| # | File Path | Purpose |
|---|-----------|---------|
| 1 | `api/models/extraction.py` | Extraction models |
| 2 | `api/services/extraction.py` | Extraction logic |
| 3 | `api/graph/client.py` | Neo4j client |
| 4 | `api/graph/writer.py` | Graph writes |
| 5 | `api/worker/entry.py` | ARQ entry |
| 6 | `api/worker/jobs.py` | Extraction job |
| 7 | `api/routers/extraction.py` | Endpoints |
| 8 | `prompts/extraction/v1.yaml` | Prompt |
| 9 | `ui/pages/extraction.py` | Phase 3 UI |
| 10 | `api/routers/monitoring.py` | Worker monitoring (update) |
| 11 | `ui/pages/monitoring.py` | Worker panel (update) |
| 12 | `tests/unit/test_extraction_validation.py` | Output validation |
| 13 | `tests/unit/test_dedupe_keys.py` | Dedupe keys |
| 14 | `tests/unit/test_worker_jobs.py` | Job serialization |
| 15 | `tests/integration/test_graph_write.py` | Neo4j write + dedupe |

---

## Acceptance Criteria

- [ ] Jobs enqueue and execute in ARQ worker
- [ ] Extracted entities conform to locked schema types
- [ ] Malformed LLM output rejected (fail-closed)
- [ ] MERGE on dedupe keys; no duplicates on rerun
- [ ] Schema cached during extraction
- [ ] `/api/monitoring/workers` returns queue depth
- [ ] `/api/monitoring/jobs/{run_id}` shows per-chunk status
- [ ] Monitoring UI shows extraction progress
- [ ] `pyproject.toml` version `0.4.0`
- [ ] SKILL-B governance checklist passes
