"""
api/audit/models.py — Immutable audit record model (SPEC-06 S-06.5).

AuditRecord captures the complete, tamper-evident record of a single governed
graph mutation event.  Records are written once by Agent-C (via
api/audit/writer.py) after successful diff application and are never modified.

Each record carries:
  - Full linkage to the governed run, schema, candidate, proposal, and diff.
  - The approval_id issued by the Approval Gate — required before any mutation.
  - A UTC ISO-8601 timestamp at microsecond resolution.
  - The actor identity (human user or agent identifier).
  - The execution outcome and, on failure, a structured error payload.

AuditRecord is frozen (immutable) — this is enforced at the Python level.
Immutability at the storage level is guaranteed by append-only S3 writes in
api/audit/writer.py; no code path reads back an existing record and overwrites
it.

record_id derivation:
    record_id = SHA-256(canonical_json({
        "run_id": ...,
        "proposal_id": ...,
        "diff_id": ...,
        "approval_id": ...,
        "timestamp_utc": ...,
    }))

record_id is a @computed_field — never accepted as constructor input.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


# ---------------------------------------------------------------------------
# Vocabulary enums
# ---------------------------------------------------------------------------


class AuditOutcome(StrEnum):
    """The execution outcome recorded in an AuditRecord.

    applied:   Diff was applied successfully; graph is now updated.
    failed:    Diff application failed; graph is unchanged.  error_detail
               will contain the structured failure information.
    rejected:  Execution was rejected before touching the graph (e.g. missing
               approval_id, schema version mismatch, invariant check failure).
    """

    applied = "applied"
    failed = "failed"
    rejected = "rejected"


# ---------------------------------------------------------------------------
# AuditRecord — immutable, append-only mutation record
# ---------------------------------------------------------------------------


class AuditRecord(BaseModel):
    """Immutable, append-only record of a single governed graph mutation event.

    Written by Agent-C via api/audit/writer.py after diff application.  Once
    written to S3, a record is never updated or deleted.

    Attributes:
        run_id:          Governed run this mutation belongs to.
        schema_version:  Domain schema version_hash in effect at execution time.
        candidate_id:    Candidate that motivated the original proposal.
        proposal_id:     SHA-256 identity of the ProposalPacket.
        diff_id:         SHA-256 identity of the DiffPlan that was applied.
        approval_id:     Token issued by the Approval Gate authorising this diff.
                         Must be present and non-empty; execution is rejected
                         without it (CLAUDE.md §4.4).
        timestamp_utc:   UTC datetime at which Agent-C began execution.
        actor:           Identity of the approving human or system agent.
        outcome:         Result of the execution attempt.
        steps_applied:   Number of DiffSteps successfully applied (≥ 0).
        error_detail:    Structured error payload on failed/rejected outcomes.
                         Empty dict on success.
        record_id:       SHA-256 of (run_id, proposal_id, diff_id, approval_id,
                         timestamp_utc).  Computed, read-only.
    """

    model_config = ConfigDict(frozen=True)

    # ---- Linkage ----
    run_id: str
    schema_version: str
    candidate_id: str
    proposal_id: str
    diff_id: str
    approval_id: str

    # ---- Execution metadata ----
    timestamp_utc: datetime
    actor: str
    outcome: AuditOutcome
    steps_applied: int = Field(ge=0, default=0)
    error_detail: dict[str, Any] = Field(default_factory=dict)

    # ---- Validators ----

    @field_validator("run_id", "schema_version", "candidate_id", "actor")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v

    @field_validator("proposal_id", "diff_id")
    @classmethod
    def _sha256_length(cls, v: str) -> str:
        if len(v) != 64:
            raise ValueError(
                f"must be a 64-character SHA-256 hex digest, got {len(v)} chars"
            )
        return v

    @field_validator("approval_id")
    @classmethod
    def _approval_id_non_empty(cls, v: str) -> str:
        """Fail-closed: execution without an approval_id is categorically rejected."""
        if not v.strip():
            raise ValueError(
                "approval_id must be non-empty — execution without an approval_id "
                "is prohibited (CLAUDE.md §4.4)"
            )
        return v

    @field_validator("timestamp_utc")
    @classmethod
    def _utc_timezone(cls, v: datetime) -> datetime:
        """Require an explicit UTC timezone — naive datetimes are rejected."""
        if v.tzinfo is None:
            raise ValueError("timestamp_utc must be timezone-aware (UTC)")
        # Normalise to UTC if a different tz was supplied.
        return v.astimezone(timezone.utc)

    # ---- Computed identity ----

    @computed_field  # type: ignore[prop-decorator]
    @property
    def record_id(self) -> str:
        """Deterministic SHA-256 hex digest identifying this audit record.

        Identity inputs:
            run_id, proposal_id, diff_id, approval_id, timestamp_utc (ISO-8601)

        timestamp_utc is included so that two executions of the same diff (e.g.
        a retry after a transient failure) produce distinct record_ids.

        Canonical JSON (sort_keys, compact, ensure_ascii) for byte-stable
        serialisation.
        """
        identity: dict[str, str] = {
            "run_id": self.run_id,
            "proposal_id": self.proposal_id,
            "diff_id": self.diff_id,
            "approval_id": self.approval_id,
            "timestamp_utc": self.timestamp_utc.isoformat(),
        }
        serialized = json.dumps(
            identity,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    # ---- Convenience constructors ----

    @classmethod
    def make(
        cls,
        *,
        run_id: str,
        schema_version: str,
        candidate_id: str,
        proposal_id: str,
        diff_id: str,
        approval_id: str,
        actor: str,
        outcome: AuditOutcome,
        steps_applied: int = 0,
        error_detail: dict[str, Any] | None = None,
    ) -> "AuditRecord":
        """Convenience constructor that stamps timestamp_utc = now (UTC).

        Prefer this constructor in Agent-C over direct instantiation so that
        the timestamp is always captured at execution time, not at model
        definition time.
        """
        return cls(
            run_id=run_id,
            schema_version=schema_version,
            candidate_id=candidate_id,
            proposal_id=proposal_id,
            diff_id=diff_id,
            approval_id=approval_id,
            timestamp_utc=datetime.now(tz=timezone.utc),
            actor=actor,
            outcome=outcome,
            steps_applied=steps_applied,
            error_detail=error_detail or {},
        )
