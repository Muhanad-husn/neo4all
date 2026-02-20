# SKILL-A: Full-Stack API Contract Discipline

**Applies to**: Every increment that introduces or modifies an API endpoint.
**Authority**: This skill is referenced from CLAUDE.md Section 17.

---

## Purpose

Every API boundary in this project is defined by typed Pydantic models. This skill ensures contracts are explicit, validated at boundaries, and committed alongside route handlers.

---

## Rules

### R-A1: Pydantic Models Are Mandatory

Every FastAPI endpoint must have:
- A **request model** (Pydantic `BaseModel` subclass) defining the accepted input
- A **response model** (Pydantic `BaseModel` subclass) defining the returned output

Models are defined in a `models.py` file within the relevant `api/` subdirectory. They are never defined inline in route handlers.

### R-A2: Standard Response Envelope

All response models must include:
```python
class BaseResponse(BaseModel):
    run_id: str
    status: Literal["success", "error", "partial"]
    errors: list[ErrorDetail] = []

class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None
```

Typed payloads extend `BaseResponse` with their specific fields.

### R-A3: Inter-Service Contracts

All calls between `api/` submodules (e.g., `api/services/` calling `api/graph/`) must pass typed Pydantic objects. Raw dicts are banned at module boundaries.

### R-A4: Validation at the Boundary

Invalid payloads are rejected at the router layer with a structured error response. The system never silently coerces, patches, or passes through invalid input.

### R-A5: Co-Located Commits

When a new endpoint is added, its request/response models must exist in the same commit as the route handler. Models are never deferred to a later commit.

### R-A6: OpenAPI Accuracy

The auto-generated OpenAPI schema (FastAPI `/docs`) must remain accurate. No manual overrides. If the OpenAPI output is wrong, fix the Pydantic models — not the schema.

---

## Claude Code Procedure

When implementing any endpoint:
1. Create or update the `models.py` file in the target `api/` subdirectory
2. Define request and response models extending `BaseResponse`
3. Write the route handler referencing these models
4. Write a unit test asserting that malformed input is rejected with a structured error
5. Verify the FastAPI `/docs` page reflects the new endpoint correctly

---

## Verification Checklist

- [ ] Every route has a Pydantic request model
- [ ] Every route has a Pydantic response model extending `BaseResponse`
- [ ] No raw dicts cross module boundaries
- [ ] Invalid input returns structured `ErrorDetail`, not a 500
- [ ] Models file exists in the same `api/` subdirectory as the route
