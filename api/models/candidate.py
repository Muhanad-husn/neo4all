"""
api/models/candidate.py — Curation candidate model (SPEC-05 S-05.2).

Defines the Candidate model representing a single curation work item detected
by one of the five deterministic candidate detectors in SPEC-05 S-05.1:

  - CandidateType:   Which class of data quality issue was detected.
  - CandidateLane:   Grouping lane for UI presentation and agent routing.
  - Severity:        Urgency signal for prioritisation.
  - Candidate:       Immutable record with deterministic candidate_id.

Identity contract (CLAUDE.md §5):
    candidate_id = SHA-256(canonical_json({
        "run_id": ...,
        "schema_version": ...,
        "candidate_type": ...,
        "involved_element_refs": sorted(...),
        "detection_method": ...
    }))

collision_context and severity are deliberately excluded from candidate_id:
they describe the nature of the issue but are not part of the unique identity
of the candidate situation in the graph.  Two detectors observing the same
graph situation with different contextual annotations or different severity
assessments produce the same candidate_id.

candidate_id is a @computed_field — it is never accepted as constructor input
and is always re-derived from the model's stored fields.  Because the model
is frozen, the computed value is stable for the object's lifetime.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


# ---------------------------------------------------------------------------
# Vocabulary enums
# ---------------------------------------------------------------------------


class CandidateType(StrEnum):
    """Which class of data quality issue this candidate represents.

    Values correspond directly to the five detectors in SPEC-05 S-05.1.

    exact_node_duplicate:  Same NodeType + same dedupe_key under different IDs.
    exact_rel_duplicate:   Same rel type + same start/end + same identity key.
    probable_duplicate:    Blocking key match + Jaro-Winkler similarity + shared
                           context score above threshold.
    canonical_violation:   Schema-defined direction or inverse mapping violated.
    structural_anomaly:    Orphan node, degree outlier, missing provenance, or
                           qualifier inconsistency.
    """

    exact_node_duplicate = "exact_node_duplicate"
    exact_rel_duplicate = "exact_rel_duplicate"
    probable_duplicate = "probable_duplicate"
    canonical_violation = "canonical_violation"
    structural_anomaly = "structural_anomaly"


class CandidateLane(StrEnum):
    """Grouping lane used by the curation UI and agent router.

    node:         Candidate involves node identity (duplication, naming).
    relationship: Candidate involves edge identity or direction.
    structural:   Candidate involves graph topology (orphans, degree outliers,
                  missing provenance).
    """

    node = "node"
    relationship = "relationship"
    structural = "structural"


class Severity(StrEnum):
    """Urgency signal — used for prioritisation in the curation UI.

    critical: Must be resolved before any agent pipeline can proceed.
    high:     Should be resolved; likely to cause downstream errors.
    medium:   Worth curating; may affect query quality.
    low:      Informational; low impact on graph quality.
    """

    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


# ---------------------------------------------------------------------------
# Candidate — immutable curation work item
# ---------------------------------------------------------------------------


class Candidate(BaseModel):
    """Immutable record of a curation candidate detected by a deterministic detector.

    candidate_id is a @computed_field derived from:
        (run_id, schema_version, candidate_type,
         sorted(involved_element_refs), detection_method)

    It is never accepted as constructor input and is always re-derived from
    the model's stored fields.  Because the model is frozen, the computed
    value is stable for the object's lifetime.

    Identity contract (CLAUDE.md §5):
        candidate_id = SHA-256(canonical_json(above fields))

    Canonical JSON uses sort_keys=True, compact separators, ensure_ascii=True
    for byte-stable serialisation regardless of dict insertion order, Python
    version, or process restart.  involved_element_refs is sorted so that
    candidate identity is order-independent.

    collision_context carries supplementary evidence explaining why the
    candidate was flagged (e.g. similarity scores, provenance gaps, degree
    values).  It is excluded from candidate_id.  The dict attribute itself is
    mutable by value — treat as read-only after construction (frozen=True
    prevents field reassignment, not dict mutation).

    Attributes:
        run_id:                 Governed run this candidate belongs to.
        schema_version:         Schema version_hash at detection time.
        candidate_type:         Which detector class raised this candidate.
        candidate_lane:         UI grouping lane.
        collision_context:      Supplementary evidence dict (not identity-forming).
        involved_element_refs:  Tuple of dedupe_key strings for the affected graph
                                elements (nodes, relationships, or both).  At least
                                one reference is required.
        severity:               Urgency signal for prioritisation.
        detection_method:       Stable identifier for the specific detector/rule
                                that raised this candidate (e.g.
                                "exact_node_dedupe_key", "jaro_winkler_0.9").
        candidate_id:           Deterministic 64-char SHA-256 hex digest
                                (computed, read-only).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    schema_version: str
    candidate_type: CandidateType
    candidate_lane: CandidateLane
    collision_context: dict[str, Any] = Field(default_factory=dict)
    involved_element_refs: tuple[str, ...]
    severity: Severity
    detection_method: str

    @field_validator("run_id", "schema_version", "detection_method")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v

    @field_validator("involved_element_refs")
    @classmethod
    def _at_least_one_ref(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(v) == 0:
            raise ValueError(
                "involved_element_refs must contain at least one element reference"
            )
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def candidate_id(self) -> str:
        """SHA-256 hex digest of the canonical candidate identity inputs.

        Inputs serialised into the hash:
            run_id, schema_version, candidate_type (as str),
            sorted(involved_element_refs), detection_method.

        collision_context is excluded — descriptive metadata, not identity.
        severity is excluded — prioritisation signal, not identity.

        Canonical JSON serialisation (sort_keys, no whitespace, ensure_ascii)
        provides unambiguous field boundaries.  involved_element_refs is sorted
        to guarantee order-independence: the same set of affected elements
        always produces the same candidate_id regardless of detection traversal
        order.
        """
        identity: dict[str, Any] = {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "candidate_type": str(self.candidate_type),
            "involved_element_refs": sorted(self.involved_element_refs),
            "detection_method": self.detection_method,
        }
        serialized = json.dumps(
            identity,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
