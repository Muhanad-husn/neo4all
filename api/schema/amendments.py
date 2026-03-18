"""
api/schema/amendments.py — Schema Additive Amendment models (supplementary layer).

During curation, canonical violation candidates may indicate that the schema
was incomplete rather than the data being incorrect.  Instead of normalizing
or deleting the violating relationship, the user can *amend* the schema by
adding a new EdgeTypeDef that legitimises the observed direction.

Amendments live alongside the locked schema.  They do NOT modify the
SchemaVersion object or its version_hash — all existing graph elements,
candidates, proposals, and audit records remain valid.

The CanonicalViolationDetector checks both base schema edges AND amendment
edges when building its valid_forward set.

Identity (CLAUDE.md §5):
    amendment_id = SHA-256(run_id + NUL + edge.type + NUL + edge.start_node_type + NUL + edge.end_node_type)

Deterministic — no uuid4().
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, computed_field

from api.schema.models import EdgeTypeDef


class SchemaAmendment(BaseModel, frozen=True):
    """A single additive schema amendment — one new EdgeTypeDef accepted during curation.

    Attributes:
        run_id:              Governed run identifier.
        edge_type_def:       The new edge direction being accepted.
        source_candidate_id: The canonical_violation candidate that prompted this.
        actor:               Who approved the amendment.
        timestamp_utc:       ISO-8601 timestamp of approval.
        rationale:           Human-provided justification for the amendment.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    edge_type_def: EdgeTypeDef
    source_candidate_id: str
    actor: str
    timestamp_utc: str
    rationale: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def amendment_id(self) -> str:
        """SHA-256 hex digest deterministically derived from the edge definition.

        amendment_id = SHA-256(run_id + NUL + edge.type + NUL + edge.start_node_type + NUL + edge.end_node_type)
        """
        payload = (
            f"{self.run_id}\x00"
            f"{self.edge_type_def.type}\x00"
            f"{self.edge_type_def.start_node_type}\x00"
            f"{self.edge_type_def.end_node_type}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SchemaAmendmentRegistry(BaseModel, frozen=True):
    """Append-only registry of schema amendments for a run.

    Stored in Redis at CacheKey.schema_amendments(run_id) with no TTL.

    Attributes:
        run_id:     Governed run identifier.
        amendments: Ordered tuple of amendments (newest last).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    amendments: tuple[SchemaAmendment, ...] = ()
