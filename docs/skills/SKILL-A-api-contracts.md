# SKILL-A: API Contracts — Verification Checklist

**Full rules**: See CLAUDE.md §18 (SKILL-A section)

## Verification Checklist

Run after every increment that introduces or modifies endpoints:

- [ ] Every route has a Pydantic request model
- [ ] Every route has a Pydantic response model extending `BaseResponse`
- [ ] No raw dicts cross module boundaries
- [ ] Invalid input returns structured `ErrorDetail`, not a 500
- [ ] Models file exists in the same `api/` subdirectory as the route
- [ ] OpenAPI `/docs` page accurately reflects all endpoints

## Procedure (When Implementing Endpoints)

1. Create or update `models.py` in the target `api/` subdirectory
2. Define request and response models extending `BaseResponse`
3. Write the route handler referencing these models
4. Write a unit test asserting malformed input is rejected with structured error
5. Verify FastAPI `/docs` reflects the new endpoint
