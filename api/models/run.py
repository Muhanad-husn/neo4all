"""
api/models/run.py — Domain model for a single extraction/curation session.

run_id derivation
-----------------
run_id is deterministic: sha256(user_namespace + timestamp_seed).
The null-byte separator prevents boundary collisions where a different split of
the same raw bytes would otherwise produce the same digest, e.g.:
    ("ab", "c")  vs  ("a", "bc")

Never use uuid4() for governed artifact IDs — CLAUDE.md §5.

Phase enum
----------
Phases are strictly ordered integers (CLAUDE.md §8). The StateManager in
ui/state.py enforces forward-only transitions and imports Phase from here.
"""

import hashlib
from datetime import UTC, datetime
from enum import IntEnum

from pydantic import BaseModel


class Phase(IntEnum):
    """Strictly ordered application phases.

    Numeric values are stable — they are stored in the Run model and compared
    for ordering. Do not re-number existing entries.
    """

    INIT = 0        # Phase 0: Session Initialization
    SCHEMA = 1      # Phase 1: Domain Schema Definition
    INGESTION = 2   # Phase 2: Document Ingestion & Chunking
    EXTRACTION = 3  # Phase 3: AI-Assisted Extraction
    CURATION = 4    # Phase 4: Curation


def derive_run_id(user_namespace: str, timestamp_seed: str) -> str:
    """Return a deterministic run_id from (user_namespace, timestamp_seed).

    Args:
        user_namespace: Scopes the run to a user or project context.
                        E.g. "alice", "acme-corp/project-x".
        timestamp_seed: A stable string representing the session start time.
                        Must be identical across calls to reproduce the same
                        run_id. Typically an ISO 8601 UTC string provided once
                        at session creation and then stored.

    Returns:
        64-character lowercase hex digest (SHA-256).

    Example:
        >>> derive_run_id("alice", "2026-02-21T00:00:00Z")
        'a3f2...'   # deterministic; same call always returns the same value
    """
    raw = f"{user_namespace}\x00{timestamp_seed}".encode()
    return hashlib.sha256(raw).hexdigest()


class Run(BaseModel):
    """Immutable record of a single extraction/curation session.

    All fields except schema_version are set at creation time and do not
    change. schema_version starts as None and is populated when the user locks
    the domain schema in Phase 1.
    """

    run_id: str
    schema_version: str | None = None
    phase: Phase = Phase.INIT
    created_at: datetime

    @classmethod
    def create(cls, user_namespace: str, timestamp_seed: str) -> "Run":
        """Create a new Run with a deterministically derived run_id.

        Args:
            user_namespace: User or project scope string.
            timestamp_seed: Stable timestamp string used as an ID seed.
                            Caller is responsible for capturing this once and
                            persisting it — do not pass datetime.now() on each
                            call if you need to reconstruct the same run_id.

        Returns:
            A new Run instance in Phase.INIT with created_at = utcnow().
        """
        return cls(
            run_id=derive_run_id(user_namespace, timestamp_seed),
            phase=Phase.INIT,
            created_at=datetime.now(UTC),
        )
