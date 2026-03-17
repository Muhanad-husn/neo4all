"""
api/common/prompts.py — Shared prompt template loader.

Canonical implementation of YAML prompt template loading used by all services
and agents.  Extracted per SKILL-B R-B7 (deduplicated from api/services/extraction.py,
api/schema/service.py, and api/agents/evidence.py).

Path resolution: prompts/{job_id}/{template_version}.yaml, rooted at PROMPTS_DIR
env var or the repository root /prompts/ directory.

Required template keys: ``system_prompt``, ``user_template``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROMPTS_ROOT: Path = (
    Path(os.environ["PROMPTS_DIR"])
    if "PROMPTS_DIR" in os.environ
    else Path(__file__).resolve().parents[2] / "prompts"
)


def load_prompt_template(job_id: str, template_version: str) -> dict[str, Any]:
    """Load and parse a YAML prompt template from prompts/{job_id}/{version}.yaml.

    Args:
        job_id:            Directory name under the prompts root (e.g. "extraction").
        template_version:  File stem (e.g. "v1") — resolved as ``{version}.yaml``.

    Returns:
        Dict with at least ``system_prompt`` and ``user_template`` keys.

    Raises:
        FileNotFoundError: Template file does not exist.
        ValueError:        YAML is not a mapping or missing required keys.
    """
    path = PROMPTS_ROOT / job_id / f"{template_version}.yaml"
    if not path.is_file():
        raise FileNotFoundError(
            f"Prompt template not found: {path}. "
            "Ensure prompts/{job_id}/{template_version}.yaml exists."
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not isinstance(data, dict):
        raise ValueError(f"Prompt template {path} must be a YAML mapping.")
    for required in ("system_prompt", "user_template"):
        if required not in data:
            raise ValueError(
                f"Prompt template {path} is missing required key '{required}'."
            )
    return data  # type: ignore[return-value]
