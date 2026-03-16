"""
api/agents/execution.py — Agent-C: tools-only execution of approved diffs (SPEC-06 S-06.4).

Agent-C translates an approved DiffPlan into typed graph mutations, verifies
post-apply invariants, then records an immutable AuditRecord.  It performs no
reasoning — every decision is mechanical and deterministic.

Pre-execution validation (fail-closed — CLAUDE.md §4.4):
  1. approval_id must exist in Redis (ApprovalRecord written by Approval Gate).
  2. ApprovalRecord.proposal_id must match diff.proposal_id.
  3. Proposal state must be ProposalState.approved (ProposalService).
  4. diff.schema_version must match the current locked schema (SchemaService).

Execution:
  - Steps applied in declaration order (DiffPlan ordering contract).
  - On step failure: halt, write AuditRecord with outcome=failed.
  - On invariant failure: write AuditRecord with outcome=failed.

Post-execution (on success only):
  - Invalidate all run-scoped graph cache keys (SKILL-D R-D10).
  - Write AuditRecord with outcome=applied.

Parameterization contract:
  All data values are Cypher $parameters.  Only node labels, relationship types,
  and property names are embedded in Cypher — after validation against the
  safe-identifier regex.  Validation failure raises ExecutionInjectionError
  before any query is assembled.

ApprovalRecord storage contract:
  Stored at CacheKey.approval(approval_id) with no TTL (run lifetime).
  Written by api/routers/approvals.py; read-only in this module.
"""

from __future__ import annotations

import asyncio
import time as _time
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from api.audit.models import AuditOutcome, AuditRecord
from api.audit.writer import AuditWriter
from api.cache.client import CacheClient, get_cache_client
from api.cache.keys import CacheKey
from api.config import get_settings
from api.diff.models import DiffOperation, DiffPlan, DiffStep
from api.graph.client import Neo4jClient, get_neo4j_client
from api.graph.safety import (
    ExecutionInjectionError,
    _CREATE_EDGE_T,
    _CREATE_NODE_T,
    _DELETE_EDGE,
    _DELETE_NODE,
    _EDGE_EXISTS,
    _MERGE_IN_T,
    _MERGE_OUT_T,
    _NODE_EXISTS,
    _OBS_IN_RELS,
    _OBS_OUT_RELS,
    _REMOVE_EDGE_PROP,
    _REMOVE_NODE_PROP,
    _SAFE_IDENTIFIER_RE,
    _SAFE_PROP_RE,
    _SURVIVOR_REL_GOVERNANCE_CHECK,
    _UPDATE_EDGE,
    _UPDATE_NODE,
    _extract_chunk_id,
    _rel_dedupe_key,
    _vi,
    _vp,
)
from api.graph.reader import GraphReader, get_graph_reader
from api.observability.logger import get_logger
from api.proposals.models import ProposalState
from api.proposals.service import ProposalService
from api.schema.service import SchemaService, get_schema_service
from api.services.curation.candidates import run_scoped_detection
from api.storage.artifacts import ArtifactsService, get_artifacts_service

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# ApprovalRecord — written by Approval Gate, read-only here
# ---------------------------------------------------------------------------


class _CooldownRecord(BaseModel):
    """Redis marker indicating a recent graph mutation for a run.

    Stored at CacheKey.execution_cooldown(run_id) with TTL equal to
    EXECUTION_COOLDOWN_SECONDS.  Candidate generation endpoints check this
    key and return 429 while the cooldown is active, preventing stale reads
    from Aura read replicas that haven't propagated the mutation yet.
    """

    timestamp: float
    cooldown_seconds: int


class _MergeAffectedCache(BaseModel):
    """Redis cache envelope for merge-affected scoped detection results.

    Used by _run_scoped_detection to store candidates found after a merge
    so they can be retrieved by the curation UI.
    """

    candidates: list[dict[str, Any]]


class ApprovalRecord(BaseModel):
    """Approval record stored at CacheKey.approval(approval_id).

    The Approval Gate (api/routers/approvals.py) writes this record when a
    human approves a proposal.  Agent-C fetches it to confirm that the
    approval_id is valid and refers to the correct proposal before touching
    the graph.

    Attributes:
        approval_id:  Unique token issued by the Approval Gate.
        proposal_id:  64-char SHA-256 of the approved ProposalPacket.
        run_id:       Governed run the proposal belongs to.
        actor:        Identity of the approving human operator.
        is_confirmed: True once the second phase is confirmed for high-risk proposals.
    """

    model_config = ConfigDict(frozen=True)

    approval_id: str
    proposal_id: str
    run_id: str
    actor: str
    is_confirmed: bool = False

    @field_validator("approval_id", "proposal_id", "run_id", "actor")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return v


# ---------------------------------------------------------------------------
# Execution exceptions
# ---------------------------------------------------------------------------


class ExecutionValidationError(Exception):
    """Pre-execution validation failed — graph was not touched.

    Attributes:
        code:    Machine-readable error code.
        message: Human-readable description.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ExecutionStepError(Exception):
    """A single DiffStep failed during application."""


class ExecutionInvariantError(Exception):
    """A post-apply invariant check detected an unexpected graph state."""


# ---------------------------------------------------------------------------
# ExecutionAgent — Agent-C
# ---------------------------------------------------------------------------


class ExecutionAgent:
    """Tools-only Agent-C: applies approved DiffPlans to the graph.

    No reasoning.  Every action is deterministic and derived solely from the
    approved DiffPlan and the graph's current state.

    The agent must be instantiated after the Neo4j driver has been opened
    (FastAPI lifespan).  All dependencies are injectable for unit testing.

    Args:
        neo4j_client:     Open Neo4jClient singleton.
        schema_service:   SchemaService for schema version validation.
        proposal_service: ProposalService for proposal state validation.
        audit_writer:     AuditWriter for immutable audit records.
        cache:            CacheClient for approval lookup and cache invalidation.
    """

    def __init__(
        self,
        neo4j_client: Neo4jClient | None = None,
        schema_service: SchemaService | None = None,
        proposal_service: ProposalService | None = None,
        audit_writer: AuditWriter | None = None,
        cache: CacheClient | None = None,
    ) -> None:
        self._neo4j = neo4j_client or get_neo4j_client()
        self._schema = schema_service or get_schema_service()
        self._proposals = proposal_service or ProposalService()
        self._auditor = audit_writer or AuditWriter()
        self._cache = cache or get_cache_client()
        self._merge_affected_keys: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        diff: DiffPlan,
        approval_id: str,
        actor: str,
    ) -> AuditRecord:
        """Apply an approved DiffPlan and record the outcome as an AuditRecord.

        Validates pre-conditions, applies DiffSteps in declaration order,
        checks post-apply invariants, invalidates run-scoped cache on success,
        then appends an immutable AuditRecord.  Always returns an AuditRecord —
        failures are recorded rather than raised to the caller.

        Args:
            diff:        Approved DiffPlan produced by the DiffBuilder.
            approval_id: Token issued by the Approval Gate for this proposal.
            actor:       Identity of the approving human operator.

        Returns:
            AuditRecord with outcome=applied, failed, or rejected.

        Log events:
            dry_run_skipped          INFO    — proposal_id, run_id, diff_id, steps_count, diff_operations
            dry_run_diff_stored      INFO    — run_id, diff_id, s3_key
            dry_run_diff_store_failed WARNING — run_id, diff_id, error
            execution_rejected       WARNING — proposal_id, run_id, code
            execution_step_failed    ERROR   — proposal_id, run_id, step_index, operation, error
            execution_invariant_failed ERROR — proposal_id, run_id, step_index, operation, error
            merge_obsolete_removed   INFO    — run_id, survivor_key, obsolete_key
            run_cache_invalidated    INFO    — run_id, keys_deleted
            execution_complete       INFO    — proposal_id, run_id, diff_id, outcome, steps_applied
        """
        # --- 1. Pre-execution validation (fail-closed) ---
        try:
            await self._validate_pre_execution(diff, approval_id)
        except ExecutionValidationError as exc:
            logger.warning(
                "execution_rejected",
                proposal_id=diff.proposal_id,
                run_id=diff.run_id,
                code=exc.code,
            )
            record = AuditRecord.make(
                run_id=diff.run_id,
                schema_version=diff.schema_version,
                candidate_id=diff.candidate_id,
                proposal_id=diff.proposal_id,
                diff_id=diff.diff_id,
                approval_id=approval_id,
                actor=actor,
                outcome=AuditOutcome.rejected,
                steps_applied=0,
                error_detail={"code": exc.code, "message": exc.message},
            )
            await self._auditor.append(record)
            return record

        # --- 2. Dry-run guard (SPEC-08 S-08.1) ---
        if get_settings().DRY_RUN:
            logger.info(
                "dry_run_skipped",
                proposal_id=diff.proposal_id,
                run_id=diff.run_id,
                diff_id=diff.diff_id,
                steps_count=len(diff.steps),
                diff_operations=[str(s.operation) for s in diff.steps],
            )

            # Persist the full DiffPlan to S3 so operators can review
            # exactly what *would* have been applied to the graph.
            try:
                artifacts = get_artifacts_service()
                s3_key = f"{diff.run_id}/dry-run/{diff.diff_id}.json"
                await artifacts.store_json(
                    s3_key, diff.model_dump_json().encode("utf-8")
                )
                logger.info(
                    "dry_run_diff_stored",
                    run_id=diff.run_id,
                    diff_id=diff.diff_id,
                    s3_key=s3_key,
                )
            except Exception as exc:
                # Best-effort: S3 failure must not block the dry-run audit
                # record from being written.
                logger.warning(
                    "dry_run_diff_store_failed",
                    run_id=diff.run_id,
                    diff_id=diff.diff_id,
                    error=str(exc),
                )

            record = AuditRecord.make(
                run_id=diff.run_id,
                schema_version=diff.schema_version,
                candidate_id=diff.candidate_id,
                proposal_id=diff.proposal_id,
                diff_id=diff.diff_id,
                approval_id=approval_id,
                actor=actor,
                outcome=AuditOutcome.applied,
                steps_applied=0,
                error_detail={"dry_run": True},
            )
            await self._auditor.append(record)
            return record

        # --- 3. Apply DiffSteps in declaration order ---
        steps_applied = 0
        error_detail: dict[str, Any] = {}
        for idx, step in enumerate(diff.steps):
            try:
                await self._apply_step(step, diff.run_id, diff.schema_version)
                steps_applied += 1
            except Exception as exc:
                logger.error(
                    "execution_step_failed",
                    proposal_id=diff.proposal_id,
                    run_id=diff.run_id,
                    step_index=idx,
                    operation=str(step.operation),
                    error=str(exc),
                )
                error_detail = {
                    "step_index": idx,
                    "operation": str(step.operation),
                    "error": str(exc),
                }
                break

        # --- 4. Post-apply invariant checks (only on full-step success) ---
        if not error_detail:
            for idx, step in enumerate(diff.steps):
                try:
                    await self._check_invariant(step, diff.run_id)
                except ExecutionInvariantError as exc:
                    logger.error(
                        "execution_invariant_failed",
                        proposal_id=diff.proposal_id,
                        run_id=diff.run_id,
                        step_index=idx,
                        operation=str(step.operation),
                        error=str(exc),
                    )
                    error_detail = {
                        "invariant_failed": True,
                        "step_index": idx,
                        "operation": str(step.operation),
                        "message": str(exc),
                    }
                    break

        outcome = AuditOutcome.applied if not error_detail else AuditOutcome.failed

        # --- 5. Post-execution cache invalidation (SKILL-D R-D10) ---
        cooldown_seconds = get_settings().EXECUTION_COOLDOWN_SECONDS
        if outcome == AuditOutcome.applied:
            await self._invalidate_run_cache(diff.run_id)
            # Store cooldown marker so candidate generation endpoints can
            # reject requests until Aura read replicas propagate the change.
            await self._cache.set(
                CacheKey.execution_cooldown(diff.run_id),
                _CooldownRecord(
                    timestamp=_time.time(),
                    cooldown_seconds=cooldown_seconds,
                ),
                ttl=cooldown_seconds,
            )

            # --- 5b. Auto-trigger scoped candidate re-detection after merge ---
            if self._merge_affected_keys:
                # Wait for Aura read replicas to propagate the mutation before
                # re-detecting candidates in the merge-affected neighborhood.
                await asyncio.sleep(cooldown_seconds)
                await self._run_scoped_detection(
                    diff.run_id, diff.schema_version, diff.diff_id
                )

        # --- 6. Write immutable audit record ---
        record = AuditRecord.make(
            run_id=diff.run_id,
            schema_version=diff.schema_version,
            candidate_id=diff.candidate_id,
            proposal_id=diff.proposal_id,
            diff_id=diff.diff_id,
            approval_id=approval_id,
            actor=actor,
            outcome=outcome,
            steps_applied=steps_applied,
            error_detail=error_detail,
        )
        await self._auditor.append(record)

        logger.info(
            "execution_complete",
            proposal_id=diff.proposal_id,
            run_id=diff.run_id,
            diff_id=diff.diff_id,
            outcome=str(outcome),
            steps_applied=steps_applied,
        )
        return record

    # ------------------------------------------------------------------
    # Pre-execution validation
    # ------------------------------------------------------------------

    async def _validate_pre_execution(self, diff: DiffPlan, approval_id: str) -> None:
        """Enforce all pre-conditions before touching the graph.

        Raises ExecutionValidationError with a machine-readable code on any
        failure.  The graph is never mutated if this method raises.
        """
        # 1. Fetch the ApprovalRecord from Redis.
        approval = await self._cache.get(
            CacheKey.approval(approval_id), model=ApprovalRecord
        )
        if approval is None:
            raise ExecutionValidationError(
                code="approval_not_found",
                message=(
                    f"Approval {approval_id!r} not found. "
                    "Issue an approval via the Approval Gate before executing."
                ),
            )
        if approval.proposal_id != diff.proposal_id:
            raise ExecutionValidationError(
                code="approval_mismatch",
                message=(
                    f"Approval {approval_id!r} was issued for proposal "
                    f"{approval.proposal_id!r}, not {diff.proposal_id!r}."
                ),
            )

        # 2. Verify the proposal is in "approved" state.
        proposal = await self._proposals.get_for_run(diff.run_id, diff.proposal_id)
        if proposal is None:
            raise ExecutionValidationError(
                code="proposal_not_found",
                message=f"Proposal {diff.proposal_id!r} not found in run {diff.run_id!r}.",
            )
        if proposal.state != ProposalState.approved:
            raise ExecutionValidationError(
                code="proposal_not_approved",
                message=(
                    f"Proposal {diff.proposal_id!r} is in state {proposal.state!r}. "
                    "Only approved proposals may be executed."
                ),
            )

        # 3. Verify schema_version matches current locked schema.
        schema = await self._schema.get_current(diff.run_id)
        if schema is None:
            raise ExecutionValidationError(
                code="schema_not_locked",
                message=f"No locked schema found for run {diff.run_id!r}.",
            )
        if schema.version_hash != diff.schema_version:
            raise ExecutionValidationError(
                code="schema_version_mismatch",
                message=(
                    f"DiffPlan schema_version {diff.schema_version!r} does not match "
                    f"current locked schema {schema.version_hash!r}."
                ),
            )

    # ------------------------------------------------------------------
    # Step dispatch
    # ------------------------------------------------------------------

    async def _apply_step(
        self, step: DiffStep, run_id: str, schema_version: str
    ) -> None:
        """Dispatch a single DiffStep to the appropriate typed handler."""
        handlers = {
            DiffOperation.update_node: self._apply_update_node,
            DiffOperation.delete_node: self._apply_delete_node,
            DiffOperation.create_node: self._apply_create_node,
            DiffOperation.update_edge: self._apply_update_edge,
            DiffOperation.delete_edge: self._apply_delete_edge,
            DiffOperation.create_edge: self._apply_create_edge,
            DiffOperation.merge_nodes: self._apply_merge_nodes,
        }
        handler = handlers.get(step.operation)
        if handler is None:
            raise ExecutionStepError(f"Unknown DiffOperation: {step.operation!r}")
        await handler(step, run_id, schema_version)

    # ------------------------------------------------------------------
    # Per-operation handlers
    # ------------------------------------------------------------------

    async def _apply_update_node(
        self, step: DiffStep, run_id: str, _sv: str
    ) -> None:
        rows = await self._run_write(
            _UPDATE_NODE,
            {"dk": step.dedupe_key, "run_id": run_id, "props": step.properties_set},
        )
        if not rows or int(rows[0].get("c", 0)) == 0:
            raise ExecutionStepError(
                f"update_node: no node found for dedupe_key={step.dedupe_key!r}"
            )
        for prop in step.properties_remove:
            cypher = _REMOVE_NODE_PROP.format(p=_vp(prop))
            await self._run_write(cypher, {"dk": step.dedupe_key, "run_id": run_id})

    async def _apply_delete_node(
        self, step: DiffStep, run_id: str, _sv: str
    ) -> None:
        await self._run_write(_DELETE_NODE, {"dk": step.dedupe_key, "run_id": run_id})

    async def _apply_create_node(
        self, step: DiffStep, run_id: str, schema_version: str
    ) -> None:
        if not step.node_labels:
            raise ExecutionStepError("create_node: node_labels is empty")
        lbl = _vi(step.node_labels[0], "node_label")
        cypher = _CREATE_NODE_T.format(lbl=lbl)
        await self._run_write(
            cypher,
            {
                "dk": step.dedupe_key,
                "props": step.properties_set,
                "run_id": run_id,
                "sv": schema_version,
            },
        )

    async def _apply_update_edge(
        self, step: DiffStep, run_id: str, _sv: str
    ) -> None:
        rows = await self._run_write(
            _UPDATE_EDGE,
            {"dk": step.dedupe_key, "run_id": run_id, "props": step.properties_set},
        )
        if not rows or int(rows[0].get("c", 0)) == 0:
            raise ExecutionStepError(
                f"update_edge: no edge found for dedupe_key={step.dedupe_key!r}"
            )
        for prop in step.properties_remove:
            cypher = _REMOVE_EDGE_PROP.format(p=_vp(prop))
            await self._run_write(cypher, {"dk": step.dedupe_key, "run_id": run_id})

    async def _apply_delete_edge(
        self, step: DiffStep, run_id: str, _sv: str
    ) -> None:
        await self._run_write(_DELETE_EDGE, {"dk": step.dedupe_key, "run_id": run_id})

    async def _apply_create_edge(
        self, step: DiffStep, run_id: str, schema_version: str
    ) -> None:
        rt = _vi(step.rel_type, "rel_type")
        cypher = _CREATE_EDGE_T.format(rt=rt)
        rows = await self._run_write(
            cypher,
            {
                "sdk": step.start_dedupe_key,
                "edk": step.end_dedupe_key,
                "dk": step.dedupe_key,
                "props": step.properties_set,
                "run_id": run_id,
                "sv": schema_version,
            },
        )
        if not rows or int(rows[0].get("c", 0)) == 0:
            raise ExecutionStepError(
                f"create_edge: endpoint nodes not found — "
                f"start={step.start_dedupe_key!r} end={step.end_dedupe_key!r}"
            )

    async def _apply_merge_nodes(
        self, step: DiffStep, run_id: str, schema_version: str
    ) -> None:
        """Collapse obsolete nodes into the survivor.

        For each obsolete node (merge_keys[1:]):
          1. Collect its outgoing and incoming relationships (excluding edges to survivor).
          2. Re-create each relationship from/to the survivor via MERGE, including
             governance metadata (_dedupe_key, _schema_version, _chunk_id).
          3. DETACH DELETE the obsolete node (removes remaining relationships).

        Affected node keys (survivor + all neighbors) are collected into
        ``self._merge_affected_keys`` for post-merge scoped candidate re-detection.
        """
        if not step.merge_keys:
            raise ExecutionStepError("merge_nodes: merge_keys is empty — no survivor defined")

        survivor_key = step.merge_keys[0]
        obsolete_keys = step.merge_keys[1:]
        affected_keys: set[str] = {survivor_key}

        for obs_key in obsolete_keys:
            affected_keys.add(obs_key)
            out_rows = await self._run_read(
                _OBS_OUT_RELS, {"ok": obs_key, "sk": survivor_key, "run_id": run_id}
            )
            in_rows = await self._run_read(
                _OBS_IN_RELS, {"ok": obs_key, "sk": survivor_key, "run_id": run_id}
            )

            for row in out_rows:
                rt = _vi(str(row["rt"]), "rel_type")
                raw_props = row.get("props") or {}
                chunk_id = _extract_chunk_id(raw_props)
                target_key = str(row["tk"])
                affected_keys.add(target_key)
                props = {
                    k: v
                    for k, v in raw_props.items()
                    if not k.startswith("_")
                }
                rdk = _rel_dedupe_key(rt, survivor_key, target_key, schema_version)
                await self._run_write(
                    _MERGE_OUT_T.format(rt=rt),
                    {
                        "sk": survivor_key,
                        "tk": target_key,
                        "props": props,
                        "run_id": run_id,
                        "rdk": rdk,
                        "sv": schema_version,
                        "chunk_id": chunk_id,
                    },
                )

            for row in in_rows:
                rt = _vi(str(row["rt"]), "rel_type")
                raw_props = row.get("props") or {}
                chunk_id = _extract_chunk_id(raw_props)
                target_key = str(row["tk"])
                affected_keys.add(target_key)
                props = {
                    k: v
                    for k, v in raw_props.items()
                    if not k.startswith("_")
                }
                rdk = _rel_dedupe_key(rt, target_key, survivor_key, schema_version)
                await self._run_write(
                    _MERGE_IN_T.format(rt=rt),
                    {
                        "sk": survivor_key,
                        "tk": target_key,
                        "props": props,
                        "run_id": run_id,
                        "rdk": rdk,
                        "sv": schema_version,
                        "chunk_id": chunk_id,
                    },
                )

            await self._run_write(_DELETE_NODE, {"dk": obs_key, "run_id": run_id})
            logger.info(
                "merge_obsolete_removed",
                run_id=run_id,
                survivor_key=survivor_key,
                obsolete_key=obs_key,
            )

        self._merge_affected_keys = list(affected_keys)

    # ------------------------------------------------------------------
    # Post-apply invariant checks
    # ------------------------------------------------------------------

    async def _check_invariant(self, step: DiffStep, run_id: str) -> None:
        """Verify the graph reflects the expected post-apply state for a step.

        Raises ExecutionInvariantError when the graph state does not match.
        """
        op = step.operation
        dk = step.dedupe_key

        if op in (DiffOperation.create_node, DiffOperation.update_node):
            if not await self._node_exists(dk, run_id):
                raise ExecutionInvariantError(
                    f"{op}: node {dk!r} not found after application"
                )
        elif op == DiffOperation.delete_node:
            if await self._node_exists(dk, run_id):
                raise ExecutionInvariantError(
                    f"delete_node: node {dk!r} still exists after deletion"
                )
        elif op in (DiffOperation.create_edge, DiffOperation.update_edge):
            if not await self._edge_exists(dk, run_id):
                raise ExecutionInvariantError(
                    f"{op}: edge {dk!r} not found after application"
                )
        elif op == DiffOperation.delete_edge:
            if await self._edge_exists(dk, run_id):
                raise ExecutionInvariantError(
                    f"delete_edge: edge {dk!r} still exists after deletion"
                )
        elif op == DiffOperation.merge_nodes:
            survivor_key = step.merge_keys[0] if step.merge_keys else dk
            if not await self._node_exists(survivor_key, run_id):
                raise ExecutionInvariantError(
                    f"merge_nodes: survivor {survivor_key!r} not found after merge"
                )
            for obs_key in step.merge_keys[1:]:
                if await self._node_exists(obs_key, run_id):
                    raise ExecutionInvariantError(
                        f"merge_nodes: obsolete node {obs_key!r} still exists after merge"
                    )
            # Verify all survivor relationships carry governance metadata.
            gov_rows = await self._run_read(
                _SURVIVOR_REL_GOVERNANCE_CHECK,
                {"sk": survivor_key, "run_id": run_id},
            )
            ungoverned = int(gov_rows[0].get("c", 0)) if gov_rows else 0
            if ungoverned > 0:
                raise ExecutionInvariantError(
                    f"merge_nodes: {ungoverned} relationship(s) on survivor "
                    f"{survivor_key!r} lack governance metadata "
                    "(_dedupe_key or _schema_version)"
                )

    # ------------------------------------------------------------------
    # Scoped candidate re-detection after merge
    # ------------------------------------------------------------------

    async def _run_scoped_detection(
        self, run_id: str, schema_version: str, diff_id: str
    ) -> None:
        """Run candidate detectors scoped to the merge-affected neighborhood.

        Best-effort: failures are logged but never block the audit record.
        Results are stored under CacheKey.merge_affected(run_id, diff_id).
        """
        try:
            reader = get_graph_reader()
            schema = await self._schema.get_current(run_id)
            if schema is None:
                logger.warning(
                    "scoped_detection_no_schema", run_id=run_id, diff_id=diff_id
                )
                return

            focus_keys = set(self._merge_affected_keys)
            candidates = await run_scoped_detection(
                run_id=run_id,
                schema_version=schema_version,
                focus_keys=focus_keys,
                reader=reader,
                schema=schema,
            )

            if candidates:
                await self._cache.set(
                    CacheKey.merge_affected(run_id=run_id, diff_id=diff_id),
                    _MergeAffectedCache(
                        candidates=[c.model_dump() for c in candidates],
                    ),
                    ttl=300,
                )

            logger.info(
                "scoped_detection_complete",
                run_id=run_id,
                diff_id=diff_id,
                focus_keys_count=len(focus_keys),
                candidates_found=len(candidates),
            )
        except Exception as exc:
            logger.warning(
                "scoped_detection_failed",
                run_id=run_id,
                diff_id=diff_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Cache invalidation
    # ------------------------------------------------------------------

    async def _invalidate_run_cache(self, run_id: str) -> None:
        """Evict all run-scoped graph cache keys after a successful mutation.

        Targets the two cache namespaces that depend on graph state:
          - gq:{run_id}:*       — GraphReader query results (5 min TTL)
          - candidates:{run_id}:* — candidate detection results (5 min TTL)

        schema:{run_id} is deliberately excluded (immutable after Phase 1 lock).
        """
        gq_deleted = await self._cache.invalidate_prefix(
            CacheKey.graph_query_prefix(run_id)
        )
        cand_deleted = await self._cache.invalidate_prefix(
            CacheKey.candidates_prefix(run_id)
        )
        logger.info(
            "run_cache_invalidated",
            run_id=run_id,
            graph_query_keys=gq_deleted,
            candidate_keys=cand_deleted,
        )

    # ------------------------------------------------------------------
    # Neo4j helpers
    # ------------------------------------------------------------------

    async def _run_read(
        self, cypher: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Execute a read transaction and return rows as list-of-dicts."""

        async def _fetch(tx: Any) -> list[dict[str, Any]]:
            result = await tx.run(cypher, **params)
            return await result.data()

        async with self._neo4j.acquire_session() as session:
            return await session.execute_read(_fetch)

    async def _run_write(
        self, cypher: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Execute a write transaction and return rows as list-of-dicts."""

        async def _tx(tx: Any) -> list[dict[str, Any]]:
            result = await tx.run(cypher, **params)
            return await result.data()

        return await self._neo4j.run_write_transaction(_tx)

    async def _node_exists(self, dedupe_key: str, run_id: str) -> bool:
        """Return True if a node with this dedupe_key exists in the run."""
        rows = await self._run_read(_NODE_EXISTS, {"dk": dedupe_key, "run_id": run_id})
        return bool(rows) and int(rows[0].get("c", 0)) > 0

    async def _edge_exists(self, dedupe_key: str, run_id: str) -> bool:
        """Return True if an edge with this dedupe_key exists in the run."""
        rows = await self._run_read(_EDGE_EXISTS, {"dk": dedupe_key, "run_id": run_id})
        return bool(rows) and int(rows[0].get("c", 0)) > 0
