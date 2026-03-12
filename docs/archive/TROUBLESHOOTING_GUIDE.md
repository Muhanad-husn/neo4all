# Claude Code: Troubleshooting & Common Patterns
## For AI-Powered Graph Extraction Platform

**Purpose**: Practical solutions to common issues when implementing increments with Claude Code.

---

## Pattern 1: Context Management

### Problem: Claude Code "Forgets" Project Rules Mid-Increment

**Symptoms:**
- Generates uuid4() after being told not to
- Accesses st.session_state directly
- Creates ad-hoc loggers instead of using centralized logger
- Violates layer boundaries

**Root Cause:**
Long conversation → context window pressure → architectural rules get deprioritized

**Solution Pattern:**

**Every 3-5 turns, inject a context reminder:**

```
Before continuing, confirm you remember:
1. Never use uuid4() for governed IDs (CLAUDE.md Section 5)
2. Never access st.session_state directly (use StateManager)
3. All logging via get_logger(__name__) from api/observability/logger
4. No business logic in ui/ (CLAUDE.md Section 4.1)

Continuing with files [X-Y].
```

**Alternative:** Start each new batch with "Read CLAUDE.md Section X" where X is the most relevant section.

---

## Pattern 2: File Generation Overflow

### Problem: Requesting Too Many Files at Once

**Symptoms:**
- Claude Code generates 15+ files in one turn
- Quality degradation in later files
- Missing imports or incomplete implementations
- Inconsistent patterns across files

**Root Cause:**
Trying to generate entire spec in one go → context overflow → shortcuts taken

**Solution Pattern:**

**Strict batch sizing:**

| Content Complexity | Max Files Per Turn |
|-------------------|-------------------|
| Simple models/config | 5 files |
| Services with logic | 3 files |
| Agents/complex algorithms | 2 files |
| Integration tests | 2 files |
| UI pages | 2 files |

**Prompt template:**
```
Generate ONLY files [X] through [Y] from the spec (N files total).
Do not proceed beyond file [Y].
After completion, stop and wait for review.
```

**If overflow happens anyway:**
```
Stop. You generated too many files.

Please generate ONLY file [X] from the spec.
Follow [specific requirement for this file].
Wait for confirmation before proceeding to file [X+1].
```

---

## Pattern 3: Layer Violation Detection

### Problem: Imports Cross Forbidden Boundaries

**Examples of violations:**
- `ui/pages/curation.py` imports `from api.graph.writer import write_node`
- `api/agents/evidence.py` imports `from api.graph.writer import merge_nodes`
- `api/routers/schema.py` imports `from ui.state import StateManager`

**Detection Prompt:**
```
Audit all import statements in files [list files].

Check for SKILL-B R-B3 violations:
- ui/ importing from api/graph/, api/agents/, api/vector/, api/diff/, api/audit/
- api/vector/ or api/agents/ (except Agent-C) importing write functions from api/graph/
- Any module importing st.session_state outside ui/state.py

Report any violations found with file and line number.
```

**Fix Pattern:**
```
File [X] has a layer violation:
Line [N]: from api.graph.writer import [function]

This violates SKILL-B R-B3: [specific rule].

Refactor to:
[Show correct architecture - e.g., UI calls API endpoint instead of direct import]
```

---

## Pattern 4: Missing Pydantic Models

### Problem: Endpoints Created Without SKILL-A Contracts

**Symptoms:**
- FastAPI endpoint with no request/response type hints
- Dict passed between modules
- OpenAPI docs show "any" for request/response

**Detection Prompt:**
```
Review all endpoints in api/routers/[router].py.

For each endpoint, verify SKILL-A R-A1 compliance:
- Request model (Pydantic BaseModel)
- Response model (extends BaseResponse)
- No raw dicts crossing module boundaries

List any endpoints missing models.
```

**Fix Pattern:**
```
Endpoint POST /api/[path] is missing Pydantic models.

Create in api/routers/[router]_models.py:
1. [Operation]Request(BaseModel) with fields: [list]
2. [Operation]Response(BaseResponse) with fields: [list]

Update endpoint signature to:
@router.post("/[path]", response_model=[Operation]Response)
async def [operation](request: [Operation]Request) -> [Operation]Response:
```

---

## Pattern 5: Non-Deterministic ID Generation

### Problem: uuid4() Used for Governed Artifacts

**Detection Prompt:**
```
Search all Python files for uuid usage:

grep -r "import uuid" .
grep -r "uuid4()" .

For each match:
- If in tests/ → acceptable
- If for run_id, doc_id, chunk_id, proposal_id, diff_id → VIOLATION
- If for request_id, session_id (non-governed) → acceptable

Report violations with file and line.
```

**Fix Pattern:**
```
File: api/models/[artifact].py
Line [N]: [artifact]_id = str(uuid.uuid4())

This violates CLAUDE.md Section 5.

Replace with deterministic derivation:
import hashlib

def generate_[artifact]_id(input1: str, input2: str) -> str:
    content = f"{input1}:{input2}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]
```

---

## Pattern 6: Cache Not Used for Expensive Reads

### Problem: Missing Cache Integration

**SKILL-D R-D8 requires caching for:**
- Locked schema
- Document manifests
- Graph reader queries
- Chunk text
- Candidate detection results

**Detection Prompt:**
```
Review [service].py for expensive read operations.

Check if the following pattern is used:
1. Build deterministic cache key via CacheKey.[type](...)
2. Try cache.get()
3. If miss, read from source (Neo4j/S3/Qdrant)
4. Store in cache with appropriate TTL

Report any expensive reads without cache integration.
```

**Fix Pattern:**
```
File: api/services/[service].py
Function: get_[resource]()

Missing cache integration. Add:

from api.cache.client import CacheClient
from api.cache.keys import CacheKey

cache = CacheClient()

async def get_[resource](run_id: str) -> [Resource]:
    # Try cache first
    key = CacheKey.[resource](run_id=run_id)
    cached = await cache.get(key, model=[Resource])
    if cached:
        logger.debug("cache_hit", key=key)
        return cached
    
    # Cache miss - read from source
    logger.debug("cache_miss", key=key)
    resource = await [read_from_source](run_id)
    
    # Store in cache
    await cache.set(key, value=resource, ttl=[TTL_FROM_CONFIG])
    
    return resource
```

---

## Pattern 7: Unstructured Logging

### Problem: Free-Text Log Messages Instead of Structured Events

**Violations:**
```python
# BAD - unstructured
logger.info(f"Extraction complete for chunk {chunk_id}, got {len(nodes)} nodes")

# BAD - logging sensitive data
logger.debug(f"Neo4j password: {password}")

# BAD - ad-hoc logger
import logging
logging.basicConfig(level=logging.INFO)
```

**Detection Prompt:**
```
Review logging calls in [files].

Check for SKILL-D compliance:
- All loggers via get_logger(__name__)
- Structured events: logger.info("event_name", key1=val1, key2=val2)
- No f-strings in log messages
- No sensitive data (credentials, API keys, raw document text)
- Correlation IDs present in context

Report violations.
```

**Fix Pattern:**
```
Replace:
logger.info(f"Extraction complete for chunk {chunk_id}, got {len(nodes)} nodes")

With:
logger.info("extraction_complete", 
    run_id=run_id,
    chunk_id=chunk_id, 
    node_count=len(nodes),
    edge_count=len(edges))
```

---

## Pattern 8: Missing Test Coverage

### Problem: Generated Code Without Tests

**Required test types per CLAUDE.md Section 10:**
- Unit tests for deterministic components
- Agent tests for policy/gateway behavior
- Integration tests for real writes

**Detection Prompt:**
```
For increment [N], verify test coverage:

From SPEC-0[N] "Files to Generate" table, identify test files.
For each test file:
1. Confirm it exists
2. Check test count (should be 3-5 tests per file)
3. Verify no network/LLM/Neo4j calls in unit tests
4. Confirm idempotency assertions for deterministic functions

Report missing or incomplete tests.
```

**Fix Pattern:**
```
File: tests/unit/test_[component].py
Status: Missing

Generate test file with coverage for:
1. Happy path (valid inputs → expected output)
2. Idempotency (same inputs called twice → same output both times)
3. Invalid inputs → fail-closed behavior (rejection, not coercion)
4. Edge cases (empty inputs, max values, boundary conditions)

Use fixtures from /fixtures/ where applicable.
```

---

## Pattern 9: Environment Variable Handling

### Problem: Missing or Incorrect Config Validation

**Common issues:**
- Optional var treated as required
- No fail-closed behavior on missing required var
- Default values that should fail instead

**Detection Prompt:**
```
Review api/config.py Settings class.

For each environment variable:
1. Is it marked required or optional correctly?
2. Does startup fail if required var missing? (CLAUDE.md Section 4.4)
3. Are sensitive vars (API keys, passwords) loaded correctly?
4. Check against CLAUDE.md Section 9 list

Report any mismatches.
```

**Fix Pattern:**
```
Environment variable NEO4J_DEV_URI is required but has default value.

Change from:
NEO4J_DEV_URI: str = "neo4j://localhost"

To:
NEO4J_DEV_URI: str  # No default - fail if missing

Startup validation in api/main.py should check:
if not settings.NEO4J_DEV_URI:
    raise ValueError("NEO4J_DEV_URI required but not set")
```

---

## Pattern 10: StateManager Bypass

### Problem: Direct st.session_state Access in UI

**Detection Prompt:**
```
Search for direct session state access:

grep -r "st.session_state\[" ui/
grep -r "st.session_state\." ui/

Any matches outside ui/state.py are violations.

Report violations with file and line.
```

**Fix Pattern:**
```
File: ui/pages/[page].py
Line [N]: current_phase = st.session_state["current_phase"]

This violates CLAUDE.md Section 8.

Replace with:
from ui.state import StateManager

state = StateManager.get()
current_phase = state.current_phase
```

---

## Recovery Strategies

### Strategy 1: Context Window Overflow Recovery

**Symptoms:**
- Responses get shorter and less detailed
- Claude Code starts making assumptions
- Quality degradation across the board

**Recovery Prompt:**
```
Context reset needed. Let's pause and reload.

Please read these files fresh:
1. /mnt/project/CLAUDE.md (focus on Sections 4, 5, 8)
2. /mnt/project/docs/specs/SPEC-0X-[name].md
3. /mnt/project/docs/skills/SKILL-[A/B/C/D]-[name].md

Current status:
- Files 1-[N] complete and reviewed
- Now resuming with file [N+1]
- Applying skills: [list]

Ready to continue?
```

### Strategy 2: Major Violation Rollback

**When to use:**
Multiple files have fundamental issues (wrong directory, layer violations, missing contracts)

**Recovery Prompt:**
```
We need to rollback files [X-Y] due to [violations].

Please:
1. Review CLAUDE.md Section [relevant] and SKILL-[X] R-[Y] rule
2. Regenerate files [X-Y] with correct:
   - Directory placement
   - Layer boundaries
   - Contract definitions
   - [other requirements]

Start with file [X] only. Wait for approval before file [X+1].
```

### Strategy 3: Incremental Fix

**When to use:**
Most files are correct, but 1-2 have issues

**Recovery Prompt:**
```
Files [X, Y] have issues:
- File [X]: [specific issue]
- File [Y]: [specific issue]

Fix only these files. Do not regenerate others.

For file [X]:
[Specific fix instructions]

For file [Y]:
[Specific fix instructions]
```

---

## Preventive Measures

### Measure 1: Start Each Session with Context Loading

**Always begin with:**
```
Starting work on SPEC-0X.

Load context:
1. Read /mnt/project/CLAUDE.md
2. Read /mnt/project/docs/specs/SPEC-0X-[name].md
3. Read all files in /mnt/project/docs/skills/

Confirm loaded and ready.
```

### Measure 2: Batch Size Discipline

**Never request more than:**
- 5 files if simple (models, config)
- 3 files if medium (services, routers)
- 2 files if complex (agents, integrations)

### Measure 3: Checkpoint Reviews

**After every 3 batches, run:**
```
Checkpoint review. Verify:
1. All files in correct directories
2. No layer violations in imports
3. All endpoints have Pydantic models
4. All modules use centralized logger
5. No uuid4() in governed IDs

Report status (PASS/FAIL per item).
```

### Measure 4: Skill Reference in Every Batch

**Each batch prompt should explicitly mention skills:**
```
Generate files [X-Y] from SPEC-0Z.

Apply:
- SKILL-A R-A1: Pydantic models
- SKILL-B R-B2: Correct directories
- SKILL-D R-D1: Centralized logger
```

---

## Quick Reference: Detection Commands

| Issue | Detection Command |
|-------|------------------|
| Layer violations | `grep -r "from api.graph.writer import" ui/` |
| Direct state access | `grep -r "st.session_state\[" ui/ \| grep -v state.py` |
| uuid4 usage | `grep -r "uuid4()" . --exclude-dir=tests` |
| Ad-hoc loggers | `grep -r "logging.basicConfig" .` |
| Missing Pydantic models | Check `@router.[method]` signatures for type hints |
| Cache misses | `grep -r "await cache.get" api/services/` |
| Unstructured logs | `grep -r 'logger.info(f"' .` |

---

## Escalation Path

If issues persist after multiple fix attempts:

1. **Step 1**: Context reset (reload CLAUDE.md + spec)
2. **Step 2**: Reduce batch size to 1 file at a time
3. **Step 3**: Generate file manually based on spec, paste for review
4. **Step 4**: Request specific skill guidance: "Show me how SKILL-X R-Y applies to this file"
5. **Step 5**: Break file into smaller functions, generate incrementally

---

## Success Indicators

You're on track when:

✅ Claude Code references CLAUDE.md sections without prompting
✅ Batch sizes stay under limits
✅ Governance checks pass on first run
✅ No context reset needed within increment
✅ Test coverage complete without reminders
✅ Version bump and docs updated automatically

---

**Version**: 1.0  
**Last Updated**: 2025-02  
**Applies To**: All increments (SPEC-01 through SPEC-08)
