# SPEC-05: Deterministic Candidate Generation (Curation Layer 1)

**Increment**: 5 | **Version target**: 0.5.0 | **Prerequisites**: SPEC-04 complete
**Skills required**: SKILL-A, SKILL-B, SKILL-D

---

## Objective

Implement pre-curation: deterministic, zero-LLM detection of duplicates, violations, and anomalies.

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
Create `api/models/candidate.py`: deterministic `candidate_id = hash(candidate_inputs + detection_method)`. Fields: candidate_type, candidate_lane, collision_context, involved_element_refs[], severity, detection_method.

### S-05.3: Graph Reader
Create `api/graph/reader.py`: read-only Neo4j queries — neighbor traversal, orphan detection, degree computation, dedupe key lookups. Cache via `CacheKey.graph_query(run_id, query_hash)` with 5-minute TTL (SKILL-D R-D8).

### S-05.4: Endpoints
Create `api/routers/curation.py`: `POST /api/curation/candidates/generate`, `GET /api/curation/candidates/{run_id}`.

### S-05.5: UI
Create `ui/pages/curation.py`: Phase 4 entry — candidates grouped by type with severity indicators.

---

## Files to Generate

| # | File Path | Purpose |
|---|-----------|---------|
| 1 | `api/models/candidate.py` | Candidate model |
| 2 | `api/services/curation/candidates.py` | Five detectors |
| 3 | `api/graph/reader.py` | Read-only graph queries |
| 4 | `api/routers/curation.py` | Candidate endpoints |
| 5 | `ui/pages/curation.py` | Phase 4 Layer 1 UI |
| 6 | `fixtures/candidate_detection/*.json` | Fixture graphs per detector |
| 7 | `tests/unit/test_candidate_*.py` | Per-detector + ID determinism |

---

## Acceptance Criteria

- [ ] All five detectors correct on fixture data
- [ ] Candidate IDs deterministic
- [ ] Zero LLM involvement
- [ ] Graph reader queries cached (5-min TTL)
- [ ] `pyproject.toml` version `0.5.0`
- [ ] SKILL-B governance checklist passes
