"""
api/proposals/service.py — Proposal Packet lifecycle management (SPEC-06 S-06.1).

ProposalService manages the full lifecycle of Proposal Packets:
  create()        — Persist a new ProposalPacket (idempotent on same proposal_id).
  get_for_run()   — Fetch a single proposal; Redis-first, S3 fallback.
  list_for_run()  — Return all proposals for a run in creation order.
  transition()    — Advance a proposal's state machine; rejects invalid moves.

State machine (enforced by api/proposals/models.py):
  pending → approved | rejected | deferred
  deferred → pending
  approved, rejected: terminal — no further transitions permitted.

Fail-closed behaviour (CLAUDE.md §4.4):
  - run_id or schema_version absent/whitespace  → ProposalValidationError
  - Requested state transition is not permitted  → InvalidTransitionError
  - Proposal not found during transition          → ProposalNotFoundError

Storage pattern:
  - Primary write: S3 at {run_id}/proposals/{proposal_id}.json (durable).
  - Cache write-through: Redis via CacheKey.proposal (no TTL — run lifetime).
  - Index: CacheKey.proposals_index per run_id (ordered list of proposal_ids).
  - Reads: Redis-first, S3 fallback, cache backfill on S3 hit.
  - Redis failures degrade gracefully — never raise on cache errors (SKILL-D R-D9).
"""

from __future__ import annotations

import asyncio
from typing import Any

import boto3

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.observability.logger import get_logger
from api.proposals.models import (
    ProposalPacket,
    ProposalState,
    allowed_transitions,
)
from api.proposals.storage import (
    ProposalStorageError,
    _ProposalIndex,
    _proposal_s3_key,
    _sync_get_proposal,
    _sync_put_proposal,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class ProposalValidationError(Exception):
    """Raised when a proposal fails structural validation before storage.

    Attributes:
        code:    Machine-readable error code (e.g. "missing_run_id").
        message: Human-readable description.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ProposalNotFoundError(Exception):
    """Raised when no proposal exists for the requested proposal_id."""

    def __init__(self, proposal_id: str) -> None:
        super().__init__(f"Proposal {proposal_id!r} not found")
        self.proposal_id = proposal_id


class InvalidTransitionError(Exception):
    """Raised when the requested state transition is not permitted.

    Carries from_state and to_state for structured error responses.
    """

    def __init__(self, from_state: ProposalState, to_state: ProposalState) -> None:
        allowed = sorted(str(s) for s in allowed_transitions(from_state))
        super().__init__(
            f"Transition {from_state!r} → {to_state!r} is not permitted. "
            f"Allowed transitions from {from_state!r}: {allowed}"
        )
        self.from_state = from_state
        self.to_state = to_state


# ProposalStorageError is re-exported from api.proposals.storage via the
# import block above.  Existing ``from api.proposals.service import
# ProposalStorageError`` paths continue to work without modification.


# ---------------------------------------------------------------------------
# ProposalService
# ---------------------------------------------------------------------------


class ProposalService:
    """Manages the lifecycle of Proposal Packets.

    Storage: S3 (durable, source of truth) + Redis (fast read cache).

    All state transitions are validated against the ProposalPacket state
    machine before any write occurs.  Invalid transitions raise
    InvalidTransitionError immediately (fail-closed).

    Args:
        settings:  Application settings singleton. Defaults to process singleton.
        s3_client: Pre-built boto3 S3 client. Inject in unit tests to avoid
                   real S3 calls.
    """

    def __init__(
        self,
        settings: Any = None,
        s3_client: Any = None,
    ) -> None:
        from api.config import get_settings

        s = settings or get_settings()
        self._bucket: str = s.S3_BUCKET_NAME
        self._cache = get_cache_client()

        if s3_client is not None:
            self._client: Any = s3_client
        else:
            self._client = boto3.client(
                "s3",
                endpoint_url=s.S3_ENDPOINT_URL,
                aws_access_key_id=s.S3_ACCESS_KEY_ID,
                aws_secret_access_key=s.S3_SECRET_ACCESS_KEY,
            )

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    async def create(self, packet: ProposalPacket) -> ProposalPacket:
        """Persist a new ProposalPacket.

        Idempotent: if a proposal with the same proposal_id already exists,
        the existing packet is returned unchanged.  The caller cannot silently
        overwrite a proposal that has already transitioned state.

        Secondary fail-closed guard: validates run_id and schema_version as
        non-empty before any I/O.  ProposalPacket.__init__ enforces these via
        field_validator, but the service-level guard makes the contract explicit
        and prevents future refactors from inadvertently bypassing it.

        Args:
            packet: A fully constructed, frozen ProposalPacket.

        Returns:
            The stored packet (the input if newly created, or the existing
            packet if this proposal_id was already present).

        Raises:
            ProposalValidationError: If run_id or schema_version are absent.
            ProposalStorageError:    On S3 write failure.

        Log events:
            proposal_created       INFO  — proposal_id, run_id, proposal_class
            proposal_already_exists INFO  — proposal_id, run_id (idempotent path)
            proposal_store_error   ERROR — proposal_id, run_id, error
        """
        if not packet.run_id.strip():
            raise ProposalValidationError(
                code="missing_run_id",
                message="run_id must be a non-empty, non-whitespace string",
            )
        if not packet.schema_version.strip():
            raise ProposalValidationError(
                code="missing_schema_version",
                message="schema_version must be a non-empty, non-whitespace string",
            )

        # Idempotency: short-circuit if the proposal already exists.
        existing = await self.get_for_run(packet.run_id, packet.proposal_id)
        if existing is not None:
            logger.info(
                "proposal_already_exists",
                proposal_id=packet.proposal_id,
                run_id=packet.run_id,
            )
            return existing

        # Write to S3 (durable, source of truth).
        s3_key = _proposal_s3_key(packet.run_id, packet.proposal_id)
        body = packet.model_dump_json().encode("utf-8")
        try:
            await asyncio.to_thread(
                _sync_put_proposal, self._client, self._bucket, s3_key, body
            )
        except ProposalStorageError as exc:
            logger.error(
                "proposal_store_error",
                proposal_id=packet.proposal_id,
                run_id=packet.run_id,
                error=exc.message,
            )
            raise

        # Write-through: cache the packet (no TTL — proposals live for run lifetime).
        await self._cache.set(CacheKey.proposal(packet.proposal_id), packet)

        # Update the per-run index.
        await self._add_to_index(packet.run_id, packet.proposal_id)

        logger.info(
            "proposal_created",
            proposal_id=packet.proposal_id,
            run_id=packet.run_id,
            proposal_class=str(packet.proposal_class),
        )
        return packet

    # ------------------------------------------------------------------
    # get_for_run
    # ------------------------------------------------------------------

    async def get_for_run(
        self,
        run_id: str,
        proposal_id: str,
    ) -> ProposalPacket | None:
        """Fetch a single proposal by ID with S3 fallback.

        Read-through pattern: Redis first → S3 fallback → backfill cache on hit.
        The run_id is required to construct the S3 key when the Redis entry is
        absent.

        Args:
            run_id:      Governed run identifier.
            proposal_id: 64-char SHA-256 hex digest of the proposal.

        Returns:
            ProposalPacket on hit, None if not found in Redis or S3.

        Log events:
            proposal_cache_miss   DEBUG — proposal_id, run_id
            proposal_fetched_s3   DEBUG — proposal_id, run_id
            proposal_not_found    DEBUG — proposal_id, run_id
        """
        cached = await self._cache.get(
            CacheKey.proposal(proposal_id), model=ProposalPacket
        )
        if cached is not None:
            return cached

        logger.debug("proposal_cache_miss", proposal_id=proposal_id, run_id=run_id)

        s3_key = _proposal_s3_key(run_id, proposal_id)
        raw = await asyncio.to_thread(
            _sync_get_proposal, self._client, self._bucket, s3_key
        )
        if raw is None:
            logger.debug("proposal_not_found", proposal_id=proposal_id, run_id=run_id)
            return None

        logger.debug("proposal_fetched_s3", proposal_id=proposal_id, run_id=run_id)
        packet = ProposalPacket.model_validate_json(raw)

        # Backfill cache so the next read is served from Redis.
        await self._cache.set(CacheKey.proposal(proposal_id), packet)
        return packet

    # ------------------------------------------------------------------
    # list_for_run
    # ------------------------------------------------------------------

    async def list_for_run(self, run_id: str) -> list[ProposalPacket]:
        """Return all proposals for a run in creation order.

        Reads the per-run proposal index from Redis.  On index cache miss,
        returns an empty list — proposals are re-discovered via create() as
        they are submitted.

        Args:
            run_id: Governed run identifier.

        Returns:
            Ordered list of ProposalPacket instances, oldest first.

        Log events:
            proposals_listed   INFO  — run_id, count
            proposal_list_miss DEBUG — run_id (index not in Redis)
        """
        index = await self._cache.get(
            CacheKey.proposals_index(run_id), model=_ProposalIndex
        )
        if index is None:
            logger.debug("proposal_list_miss", run_id=run_id)
            return []

        packets: list[ProposalPacket] = []
        for pid in index.proposal_ids:
            packet = await self.get_for_run(run_id, pid)
            if packet is not None:
                packets.append(packet)

        logger.info("proposals_listed", run_id=run_id, count=len(packets))
        return packets

    # ------------------------------------------------------------------
    # transition
    # ------------------------------------------------------------------

    async def transition(
        self,
        run_id: str,
        proposal_id: str,
        target: ProposalState,
    ) -> ProposalPacket:
        """Advance a proposal's lifecycle state.

        This is the primary enforcement point for the state machine.  All
        transitions — human or programmatic — must pass through this method.
        No code path may mutate a proposal's state field directly.

        Since ProposalPacket is frozen, transitioning produces a new model
        instance via model_copy().  The new packet is written to S3
        (overwriting the old JSON at the same key) and the Redis cache
        entry is updated atomically from the caller's perspective.

        Args:
            run_id:      Governed run identifier (needed for S3 key construction).
            proposal_id: 64-char SHA-256 hex digest.
            target:      The desired ProposalState to transition to.

        Returns:
            New ProposalPacket instance with the updated state.

        Raises:
            ProposalNotFoundError:  Proposal does not exist in Redis or S3.
            InvalidTransitionError: Transition is not permitted by the state
                                    machine (e.g. approved → pending).
            ProposalStorageError:   S3 write failure during state update.

        Log events:
            proposal_transition_rejected WARNING — proposal_id, from_state, to_state
            proposal_transitioned        INFO    — proposal_id, run_id, from, to
            proposal_transition_error    ERROR   — proposal_id, run_id, error
        """
        existing = await self.get_for_run(run_id, proposal_id)
        if existing is None:
            raise ProposalNotFoundError(proposal_id)

        if not existing.can_transition_to(target):
            logger.warning(
                "proposal_transition_rejected",
                proposal_id=proposal_id,
                from_state=str(existing.state),
                to_state=str(target),
            )
            raise InvalidTransitionError(existing.state, target)

        # ProposalPacket is frozen; model_copy() produces a new valid instance.
        # proposal_id is computed from run_id + candidate_id + proposal_class,
        # none of which change here, so the proposal_id is preserved.
        new_packet = existing.model_copy(update={"state": target})

        # Overwrite S3 record with the new state.
        s3_key = _proposal_s3_key(run_id, proposal_id)
        body = new_packet.model_dump_json().encode("utf-8")
        try:
            await asyncio.to_thread(
                _sync_put_proposal, self._client, self._bucket, s3_key, body
            )
        except ProposalStorageError as exc:
            logger.error(
                "proposal_transition_error",
                proposal_id=proposal_id,
                run_id=run_id,
                error=exc.message,
            )
            raise

        # Update Redis cache with the new packet.
        await self._cache.set(CacheKey.proposal(proposal_id), new_packet)

        logger.info(
            "proposal_transitioned",
            proposal_id=proposal_id,
            run_id=run_id,
            from_state=str(existing.state),
            to_state=str(target),
        )
        return new_packet

    # ------------------------------------------------------------------
    # clear_for_run
    # ------------------------------------------------------------------

    async def clear_for_run(self, run_id: str) -> int:
        """Delete all proposals for a run from S3 and Redis.

        Iterates the per-run proposal index, deletes each proposal's S3
        object and Redis cache entry, then removes the index itself.

        Args:
            run_id: Governed run identifier.

        Returns:
            Number of proposals deleted.

        Log events:
            proposals_cleared INFO — run_id, count
        """
        index = await self._cache.get(
            CacheKey.proposals_index(run_id), model=_ProposalIndex
        )
        if index is None:
            logger.info("proposals_cleared", run_id=run_id, count=0)
            return 0

        count = 0
        for pid in index.proposal_ids:
            # Delete from S3.
            s3_key = _proposal_s3_key(run_id, pid)
            try:
                await asyncio.to_thread(
                    self._client.delete_object,
                    Bucket=self._bucket,
                    Key=s3_key,
                )
            except Exception:
                logger.warning(
                    "proposal_delete_s3_error",
                    proposal_id=pid,
                    run_id=run_id,
                )
            # Delete from Redis cache.
            await self._cache.delete(CacheKey.proposal(pid))
            count += 1

        # Delete the index itself.
        await self._cache.delete(CacheKey.proposals_index(run_id))

        logger.info("proposals_cleared", run_id=run_id, count=count)
        return count

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _add_to_index(self, run_id: str, proposal_id: str) -> None:
        """Append proposal_id to the per-run Redis index (read-modify-write).

        The index is a _ProposalIndex stored at CacheKey.proposals_index(run_id).
        Duplicate entries are silently skipped (idempotent).
        Redis failures are swallowed (CacheClient logs at WARNING — SKILL-D R-D9).
        """
        index_key = CacheKey.proposals_index(run_id)
        existing_index = await self._cache.get(index_key, model=_ProposalIndex)
        if existing_index is None:
            existing_index = _ProposalIndex()

        if proposal_id in existing_index.proposal_ids:
            return  # already indexed — idempotent

        updated = _ProposalIndex(
            proposal_ids=[*existing_index.proposal_ids, proposal_id]
        )
        await self._cache.set(index_key, updated)
