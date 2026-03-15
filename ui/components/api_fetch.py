"""
ui/components/api_fetch.py — Shared API fetch helper for UI components.

Centralises the duplicated ``_fetch()`` pattern used by dashboard tabs,
extraction page, curation pipeline, etc.

Architecture rules:
  - Pure HTTP helper — no business logic (CLAUDE.md §4.1, SKILL-B R-B3).
  - No st.session_state access (CLAUDE.md §8).
"""

from __future__ import annotations

import os
from typing import Any

import httpx

API_BASE_URL: str = os.environ.get("API_BASE_URL", "http://localhost:8000")
_REQUEST_TIMEOUT: float = 5.0


def fetch(path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """GET {API_BASE_URL}{path} and return parsed JSON, or None on error."""
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            response = client.get(f"{API_BASE_URL}{path}", params=params)
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
    except Exception:
        return None
