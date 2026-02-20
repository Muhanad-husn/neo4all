# SKILL-C: Packaging Readiness

**Applies to**: All increments. No packaging is implemented — structure must support it.
**Authority**: This skill is referenced from CLAUDE.md Section 17.

---

## Purpose

The application will not be packaged within this project. However, the codebase must be structured so a packaging team can package it without refactoring. This skill defines the structural requirements.

---

## Rules

### R-C1: Entry Points

`pyproject.toml` must define entry points for:
- The API server (uvicorn)
- The ARQ worker

```toml
[project.scripts]
api-server = "api.main:run"
arq-worker = "api.worker.entry:run"
```

### R-C2: Absolute Imports Only

All imports must be absolute from the package root:
```python
# CORRECT
from api.services.ingestion import IngestorService
from api.graph.client import Neo4jClient

# BANNED — breaks outside the working directory
from .ingestion import IngestorService
from ..graph.client import Neo4jClient
```

### R-C3: Package Init Files

Every Python directory must contain an `__init__.py` file. Missing init files break package discovery.

### R-C4: Dependency Declarations

All dependencies must be declared in `pyproject.toml` with version bounds (`>=min,<max`). No undeclared transitive dependencies. If a module uses a library, that library must appear in `[project.dependencies]`.

### R-C5: Per-Service Dockerfiles

A `Dockerfile` must exist for each service:
- `Dockerfile.api` — FastAPI backend
- `Dockerfile.worker` — ARQ worker
- `Dockerfile.ui` — Streamlit frontend

These serve as both dev environment containers and the packaging team's starting point.

### R-C6: No Hardcoded Paths

No file path in the codebase may be hardcoded to a specific machine or container layout. All paths must be derived from environment variables or configuration.

---

## Verification Checklist

- [ ] `pyproject.toml` has `[project.scripts]` entry points
- [ ] All imports are absolute from package root
- [ ] Every Python directory has `__init__.py`
- [ ] All dependencies declared with version bounds
- [ ] Dockerfiles exist for api, worker, ui
- [ ] No hardcoded file paths
