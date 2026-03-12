# Claude Code Implementation Cheat Sheet
## AI-Powered Graph Extraction Platform

**Quick reference for implementing increments with Claude Code**

---

## 🚀 Standard Turn Sequence (Every Increment)

| # | Turn Type | Template |
|---|-----------|----------|
| 1 | Context Load | "Read CLAUDE.md + SPEC-0X + all SKILLs. Confirm: increment, version, objective, file count" |
| 2-8 | File Gen (3-5 files/turn) | "Generate files [X-Y] from SPEC-0X. Apply SKILL-[A/B/C/D]. [Specific guidance]" |
| 9 | Acceptance | "Review acceptance criteria from SPEC-0X. Verify each with file references" |
| 10 | Governance | "Run SKILL-B checklist. Report violations with file:line" |
| 11 | Tests | "Generate test files [X-Y]. Assert idempotency. No network/LLM/Neo4j in unit tests" |
| 12 | Docs | "Update pyproject (v0.X.0), README (status), CLAUDE.md (if structure changed)" |
| 13 | Verify | "Check: all 27 files exist, no lint issues, no sensitive data, entry points correct" |
| 14 | Commit | "Generate commit: feat: Increment X - [Title]. List features + version bump" |

**Total: ~14 turns per increment | Time: 30-50 minutes**

---

## 📏 Batch Size Limits (Never Exceed)

| Content Type | Max Files |
|-------------|-----------|
| Simple (models, config) | 5 |
| Medium (services, routers) | 3 |
| Complex (agents, algorithms) | 2 |
| Tests | 4 |
| UI pages | 2 |

**Rule**: If Claude generates more → Stop and reduce batch size

---

## 🎯 Essential File Paths

```
Specs:  /mnt/project/docs/specs/SPEC-0X-[name].md
Skills: /mnt/project/docs/skills/SKILL-[A|B|C|D]-[name].md
Core:   /mnt/project/CLAUDE.md
```

---

## ⚠️ Non-Negotiable Rules (CLAUDE.md Section 4)

| Rule | Quick Check |
|------|-------------|
| ❌ No st.session_state outside StateManager | `grep -r "st.session_state" ui/ \| grep -v state.py` |
| ❌ No uuid4() for governed IDs | `grep -r "uuid4()" . --exclude-dir=tests` |
| ❌ No business logic in ui/ | Check ui/ files for graph queries or agent calls |
| ❌ No LLM-generated Cypher | Check agents/ for query generation |
| ❌ No write bypasses | All mutations via Proposal → Approval → Diff → Agent-C |

---

## 🛡️ Four Skills Quick Reference

### SKILL-A: API Contracts
- ✅ Every endpoint: Pydantic request + response models
- ✅ Response extends BaseResponse
- ✅ No raw dicts between modules

### SKILL-B: Governance
- ✅ Files in correct directories (CLAUDE.md Section 2)
- ✅ No layer violations (check imports)
- ✅ Docs updated (README, CLAUDE.md if needed)
- ✅ Version bumped (MINOR for increment)

### SKILL-C: Packaging
- ✅ Absolute imports only (`from api.x import y`)
- ✅ All directories have `__init__.py`
- ✅ Entry points in pyproject.toml

### SKILL-D: Observability
- ✅ Centralized logger: `from api.observability.logger import get_logger`
- ✅ Structured events: `logger.info("event", key=val)`
- ✅ Cache before expensive reads
- ✅ Correlation IDs in all logs

---

## 🔍 Violation Detection (Copy-Paste Commands)

```bash
# Layer violations
grep -r "from api.graph.writer import" ui/

# Direct state access
grep -r "st.session_state\[" ui/ | grep -v state.py

# uuid4 in governed code
grep -r "uuid4()" . --exclude-dir=tests

# Ad-hoc loggers
grep -r "logging.basicConfig" .

# Unstructured logs
grep -r 'logger.info(f"' .

# Missing cache
grep -r "await cache.get" api/services/
```

---

## 🆘 Common Fixes

| Problem | One-Line Fix Prompt |
|---------|---------------------|
| Context overflow | "Context reset. Re-read CLAUDE.md + SPEC-0X. Status: files 1-[N] done. Continue with [N+1]" |
| Layer violation | "File [X] line [Y] violates SKILL-B R-B3. Refactor to use [correct pattern]" |
| Missing Pydantic | "Add [Op]Request + [Op]Response models to [file] per SKILL-A R-A1" |
| uuid4 used | "Replace uuid4() with hash(input1 + input2) per CLAUDE.md Section 5" |
| No cache | "Add cache integration to [function] per SKILL-D R-D8" |
| st.session_state leak | "Route [variable] through StateManager.get() per CLAUDE.md Section 8" |

---

## ✅ Checkpoint Review (Every 3 Batches)

```
Checkpoint. Verify:
1. Files in correct dirs? (CLAUDE.md Section 2)
2. Layer violations? (check imports)
3. Pydantic models? (all endpoints)
4. Centralized logger? (no ad-hoc)
5. No uuid4()? (governed IDs)

Report PASS/FAIL per item.
```

---

## 🎓 Critical Reminders

**Before Each Turn:**
- Reference the spec file explicitly
- Mention which skills apply
- State expected file count

**During Generation:**
- Max 5 files per turn
- Review before next batch
- Stop if quality degrades

**After Each Batch:**
- Check file placement
- Verify imports
- Confirm models exist

**Every Increment:**
- Run acceptance criteria
- Run SKILL-B checklist
- Bump version (0.X.0)
- Update README

---

## 🔄 Standard Prompt Templates

### Context Load
```
Read CLAUDE.md + SPEC-0X-[name].md + all SKILL files.
Confirm: increment, version, objective, file count, skills.
```

### File Generation
```
Generate files [X-Y] from SPEC-0X.
Apply SKILL-[list]. 
[Specific guidance for this batch]
Stop after file [Y].
```

### Governance Check
```
Run SKILL-B checklist.
Report violations: file:line.
```

### Commit
```
Generate commit message.
Format: "feat: Increment X - [Title]"
List features + version bump.
```

---

## 📊 Progress Tracker Template

```
Increment Status:
✅ SPEC-01: Scaffolding (v0.1.0)          - [Date]
🔄 SPEC-02: Schema Definition (v0.2.0)    - In Progress
⬜ SPEC-03: Ingestion (v0.3.0)
⬜ SPEC-04: Extraction (v0.4.0)
⬜ SPEC-05: Candidates (v0.5.0)
⬜ SPEC-06: Manual Curation (v0.6.0)
⬜ SPEC-07: Agent Pipeline (v0.7.0)
⬜ SPEC-08: Hardening (v0.8.0)
```

---

## 🎯 Success Indicators

**You're on track when:**
- ✅ Batches stay under size limits
- ✅ Claude references CLAUDE.md without prompting
- ✅ Governance checks pass first try
- ✅ No context resets needed
- ✅ Version bumped correctly
- ✅ Tests complete without reminders

**Red flags:**
- 🚩 Generating 10+ files at once
- 🚩 uuid4() appearing in code
- 🚩 Layer violations in imports
- 🚩 Missing Pydantic models
- 🚩 Direct st.session_state access
- 🚩 Ad-hoc logger creation

---

## 📞 Emergency Recovery

**If completely stuck:**

1. "Stop. Context reset. Re-read CLAUDE.md Sections 4, 5, 8"
2. Reduce to 1 file per turn
3. "Show how SKILL-X R-Y applies to this specific file"
4. Generate file in smaller pieces
5. Paste corrected version for review

---

**Print this sheet and keep it visible during implementation**

**Version**: 1.0 | **Updated**: 2025-02
