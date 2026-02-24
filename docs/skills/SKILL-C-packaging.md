# SKILL-C: Packaging — Verification Checklist

**Full rules**: See CLAUDE.md §18 (SKILL-C section)

## Verification Checklist

Run after every increment:

- [ ] `pyproject.toml` has `[project.scripts]` entry points
- [ ] All imports are absolute from package root
- [ ] Every Python directory has `__init__.py`
- [ ] All dependencies declared with version bounds
- [ ] Dockerfiles exist for api, worker, ui
- [ ] No hardcoded file paths
