"""
api/diff/models.py — Deterministic diff models (SPEC-06 S-06.2).

Defines the structured diff artifacts produced by the non-LLM Diff Builder
(api/diff/builder.py).  A DiffPlan is the complete, self-contained description
of every graph mutation required to execute an approved Proposal Packet.

Deterministic identity contract (CLAUDE.md §5):
    diff_id = SHA-256(canonical_json(diff_content))

where diff_content is the stable, sorted serialisation of the DiffPlan's
operations.  Same Proposal Packet → same DiffPlan → same diff_id on every
invocation.

diff_id is a @computed_field — never accepted as constructor input.

No Cypher is generated here.  DiffOperation values are an enum of intents;
Agent-C translates them into typed graph calls via the graph write layer.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


# ---------------------------------------------------------------------------
# DiffOperation — the set of possible atomic graph mutations
# ---------------------------------------------------------------------------


class DiffOperation(StrEnum):
    """The atomic mutation type for a single DiffStep.

    create_node:  Create a new node with the supplied labels and properties.
    update_node:  Modify properties of an existing node identified by dedupe_key.
    delete_node:  Remove an existing node (and its incident edges) by dedupe_key.
    create_edge:  Create a new directed relationship between two existing nodes.
    update_edge:  Modify properties of an existing relationship.
    delete_edge:  Remove an existing relationship by dedupe_key.
    merge_nodes:  Collapse two or more nodes into one canonical node, re-pointing
                  all incident edges to the survivor.  HIGH RISK.
    """

    create_node = "create_node"
    update_node = "update_node"
    delete_node = "delete_node"
    create_edge = "create_edge"
    update_edge = "update_edge"
    delete_edge = "delete_edge"
    merge_nodes = "merge_nodes"


# ---------------------------------------------------------------------------
# DiffStep — a single atomic mutation within a DiffPlan
# ---------------------------------------------------------------------------


class DiffStep(BaseModel):
    """One atomic graph mutation within a DiffPlan.

    Attributes:
        operation:       The kind of mutation to perform.
        dedupe_key:      The deterministic identity key for the primary target
                         element (node or relationship).
        merge_keys:      For merge_nodes: dedupe_keys of all nodes to collapse
                         into the survivor (first entry is the survivor).
        node_labels:     For create_node: Neo4j labels to apply.
        properties_set:  Properties to create or overwrite on the target.
        properties_remove: Property keys to remove from the target.
        start_dedupe_key: For edge operations: dedupe_key of the start node.
        end_dedupe_key:   For edge operations: dedupe_key of the end node.
        rel_type:        For edge operations: relationship type string.
        rationale:       Why this step is needed (copied from ProposalPacket).
        rule_ids:        Schema rule IDs that motivated this step.
    """

    model_config = ConfigDict(frozen=True)

    operation: DiffOperation

    # --- Primary target ---
    dedupe_key: str = ""

    # --- merge_nodes specific ---
    merge_keys: tuple[str, ...] = Field(default_factory=tuple)

    # --- Node creation ---
    node_labels: tuple[str, ...] = Field(default_factory=tuple)

    # --- Property mutations ---
    properties_set: dict[str, Any] = Field(default_factory=dict)
    properties_remove: tuple[str, ...] = Field(default_factory=tuple)

    # --- Edge operations ---
    start_dedupe_key: str = ""
    end_dedupe_key: str = ""
    rel_type: str = ""

    # --- Governance ---
    rationale: str = ""
    rule_ids: tuple[str, ...] = Field(default_factory=tuple)

    def _stable_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict with stable, sorted containers.

        Used by DiffPlan when computing diff_id — every mutable container is
        converted to a sorted list so that insertion-order variations do not
        produce different hashes.
        """
        return {
            "operation": str(self.operation),
            "dedupe_key": self.dedupe_key,
            "merge_keys": sorted(self.merge_keys),
            "node_labels": sorted(self.node_labels),
            "properties_set": {
                k: self.properties_set[k]
                for k in sorted(self.properties_set)
            },
            "properties_remove": sorted(self.properties_remove),
            "start_dedupe_key": self.start_dedupe_key,
            "end_dedupe_key": self.end_dedupe_key,
            "rel_type": self.rel_type,
            "rationale": self.rationale,
            "rule_ids": sorted(self.rule_ids),
        }


# ---------------------------------------------------------------------------
# DiffPlan — the complete, self-contained mutation plan for a proposal
# ---------------------------------------------------------------------------


class DiffPlan(BaseModel):
    """Deterministic, self-contained diff plan produced by the Diff Builder.

    A DiffPlan is derived entirely from an approved ProposalPacket and the
    locked domain schema.  Given the same inputs it must always produce the
    same DiffPlan and the same diff_id.

    Linkage:
        proposal_id:    SHA-256 of the ProposalPacket that generated this plan.
        run_id:         Governed run this plan is scoped to.
        candidate_id:   Candidate that motivated the original proposal.
        schema_version: Schema version_hash used during diff construction.

    Mutation plan:
        steps:          Ordered sequence of atomic DiffSteps.  Steps are
                        applied in order by Agent-C; ordering matters for
                        referential integrity (e.g. create node before edge).

    Computed:
        diff_id:        SHA-256 of canonical_json(steps), derived from the
                        stable serialisation of all steps.  Deterministic:
                        same inputs → same diff_id.
    """

    model_config = ConfigDict(frozen=True)

    proposal_id: str
    run_id: str
    candidate_id: str
    schema_version: str
    steps: tuple[DiffStep, ...] = Field(default_factory=tuple)

    @field_validator("proposal_id", "run_id", "candidate_id", "schema_version")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id_length(cls, v: str) -> str:
        if len(v) != 64:
            raise ValueError(
                f"proposal_id must be a 64-character SHA-256 hex digest, got {len(v)} chars"
            )
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def diff_id(self) -> str:
        """Deterministic SHA-256 hex digest of the diff content.

        Computed from the canonical, sorted serialisation of all DiffSteps.
        The stable serialisation of each step (via _stable_dict) ensures that
        Python dict insertion order and tuple ordering do not affect the result.

        Steps are included in the hash in their declared order — ordering is
        semantically significant (e.g. you must create a node before creating
        an edge to it).

        Same proposal → same DiffPlan → same diff_id.
        """
        diff_content: list[dict[str, Any]] = [step._stable_dict() for step in self.steps]
        serialized = json.dumps(
            diff_content,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
