"""
api/diff/builder.py — Deterministic Diff Builder (SPEC-06 S-06.2).

Translates a ProposalPacket into a DiffPlan using only the information present
in the packet itself — no LLM, no network, no randomness.  The same
ProposalPacket always produces the same DiffPlan and the same diff_id.

Dispatch table (proposal_class → DiffSteps produced):
  canonicalize  update_node / update_edge per target (ElementRef.properties as set)
  normalize     update_node / update_edge per target (same shape as canonicalize)
  rename        update_node / update_edge per target (same shape as canonicalize)
  merge         single merge_nodes step; first target is the survivor
  delete        delete_edge per rel target (sorted), then delete_node per node (sorted)
  defer         no steps (empty DiffPlan — no graph mutation)

Ordering guarantees:
  - Property-update proposals: steps sorted by dedupe_key for full determinism.
  - Delete proposals: edge steps before node steps (referential integrity),
    each group sorted by dedupe_key.
  - Merge proposals: merge_keys follow targets declaration order (caller sets
    survivor as targets[0]).

Edge cases:
  - Empty targets for any class → empty step tuple, no error (caller validates).
  - Merge with fewer than two targets → logged at WARNING, one merge_keys entry
    or zero; DiffPlan is still well-formed.
  - Unknown element_type in a target → logged at WARNING, step is skipped.

Note: The diff_id is a @computed_field on DiffPlan (api/diff/models.py) —
it is derived from the stable serialisation of all steps and does not need to
be computed here.
"""

from __future__ import annotations

from typing import Any

from api.diff.models import DiffOperation, DiffPlan, DiffStep
from api.observability.logger import get_logger
from api.proposals.models import ProposalClass, ProposalPacket

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# DiffBuilder
# ---------------------------------------------------------------------------


class DiffBuilder:
    """Non-LLM, deterministic translator from ProposalPacket to DiffPlan.

    No external dependencies.  Instantiate once and reuse — the class is
    stateless; all methods are pure functions of their arguments.

    Usage:
        builder = DiffBuilder()
        plan = builder.build(proposal)
        # plan.diff_id is stable for the same proposal inputs
    """

    def build(self, proposal: ProposalPacket) -> DiffPlan:
        """Translate a ProposalPacket into a deterministic DiffPlan.

        Dispatches to the appropriate builder method based on proposal_class.
        The resulting DiffPlan carries linkage back to the proposal, candidate,
        run, and schema, and exposes a computed diff_id.

        Args:
            proposal: A frozen ProposalPacket (any state — builder does not
                      require approval; the Approval Gate enforces that separately).

        Returns:
            DiffPlan with zero or more ordered DiffSteps.  Same input always
            returns an equivalent plan with the same diff_id.

        Log events:
            diff_built  INFO — proposal_id, run_id, proposal_class, step_count
        """
        steps = self._dispatch(proposal)
        plan = DiffPlan(
            proposal_id=proposal.proposal_id,
            run_id=proposal.run_id,
            candidate_id=proposal.candidate_id,
            schema_version=proposal.schema_version,
            steps=steps,
        )
        logger.info(
            "diff_built",
            proposal_id=proposal.proposal_id,
            run_id=proposal.run_id,
            proposal_class=str(proposal.proposal_class),
            step_count=len(steps),
        )
        return plan

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, proposal: ProposalPacket) -> tuple[DiffStep, ...]:
        """Route to the correct build method for the proposal_class."""
        dispatch: dict[ProposalClass, Any] = {
            ProposalClass.canonicalize: self._build_property_update,
            ProposalClass.normalize: self._build_property_update,
            ProposalClass.rename: self._build_property_update,
            ProposalClass.merge: self._build_merge,
            ProposalClass.delete: self._build_delete,
            ProposalClass.defer: self._build_defer,
        }
        return dispatch[proposal.proposal_class](proposal)

    # ------------------------------------------------------------------
    # canonicalize / normalize / rename
    # ------------------------------------------------------------------

    def _build_property_update(
        self, proposal: ProposalPacket
    ) -> tuple[DiffStep, ...]:
        """Produce update_node or update_edge steps for each target.

        ElementRef.properties holds the proposed target property values.
        The diff builder stamps them verbatim as properties_set so that
        Agent-C applies them to the graph element.

        Steps are sorted by dedupe_key to guarantee a stable ordering
        regardless of how targets appear in the ProposalPacket.

        Args:
            proposal: ProposalPacket with proposal_class in
                      {canonicalize, normalize, rename}.

        Returns:
            Tuple of DiffStep (one per target), sorted by dedupe_key.

        Log events:
            diff_unknown_element_type WARNING — proposal_id, dedupe_key, element_type
        """
        steps: list[DiffStep] = []
        sorted_targets = sorted(proposal.targets, key=lambda t: t.dedupe_key)

        for target in sorted_targets:
            if target.element_type == "node":
                operation = DiffOperation.update_node
            elif target.element_type == "relationship":
                operation = DiffOperation.update_edge
            else:
                logger.warning(
                    "diff_unknown_element_type",
                    proposal_id=proposal.proposal_id,
                    dedupe_key=target.dedupe_key,
                    element_type=target.element_type,
                )
                continue

            steps.append(
                DiffStep(
                    operation=operation,
                    dedupe_key=target.dedupe_key,
                    # Sort the properties dict keys so the DiffStep is stable
                    # even if the caller supplied them in arbitrary order.
                    properties_set={
                        k: target.properties[k]
                        for k in sorted(target.properties)
                    },
                    rationale=proposal.rationale,
                    rule_ids=proposal.rule_ids,
                )
            )

        return tuple(steps)

    # ------------------------------------------------------------------
    # merge
    # ------------------------------------------------------------------

    def _build_merge(self, proposal: ProposalPacket) -> tuple[DiffStep, ...]:
        """Produce a single merge_nodes step.

        The first target in proposal.targets is the survivor.  All other
        targets are collapsed into the survivor; Agent-C re-points their
        incident edges.

        Declaration order of targets is preserved in merge_keys so that the
        caller controls which node survives.  This is intentional — sorting
        merge_keys would lose the survivor designation.

        Args:
            proposal: ProposalPacket with proposal_class == merge.

        Returns:
            Tuple containing exactly one DiffStep (or empty tuple if there
            are no targets).

        Log events:
            diff_merge_insufficient_targets WARNING — proposal_id, target_count
            diff_merge_non_node_target      WARNING — proposal_id, dedupe_key, element_type
        """
        if not proposal.targets:
            logger.warning(
                "diff_merge_insufficient_targets",
                proposal_id=proposal.proposal_id,
                target_count=0,
            )
            return ()

        if len(proposal.targets) < 2:
            logger.warning(
                "diff_merge_insufficient_targets",
                proposal_id=proposal.proposal_id,
                target_count=len(proposal.targets),
            )

        node_keys: list[str] = []
        rel_keys: list[str] = []
        for target in proposal.targets:
            if target.element_type == "node":
                node_keys.append(target.dedupe_key)
            elif target.element_type == "relationship":
                rel_keys.append(target.dedupe_key)
            else:
                logger.warning(
                    "diff_merge_unknown_element_type",
                    proposal_id=proposal.proposal_id,
                    dedupe_key=target.dedupe_key,
                    element_type=target.element_type,
                )

        # Relationship-only merge: keep first, delete the rest.
        if rel_keys and not node_keys:
            steps: list[DiffStep] = []
            for key in rel_keys[1:]:  # skip survivor (first)
                steps.append(DiffStep(
                    operation=DiffOperation.delete_edge,
                    dedupe_key=key,
                    rationale=proposal.rationale,
                    rule_ids=proposal.rule_ids,
                ))
            if not steps:
                logger.warning(
                    "diff_merge_insufficient_rel_targets",
                    proposal_id=proposal.proposal_id,
                    rel_target_count=len(rel_keys),
                )
                return ()
            return tuple(steps)

        # Node merge: collapse nodes, re-point edges to survivor.
        if len(node_keys) < 2:
            logger.warning(
                "diff_merge_insufficient_node_targets",
                proposal_id=proposal.proposal_id,
                node_target_count=len(node_keys),
            )
            return ()

        step = DiffStep(
            operation=DiffOperation.merge_nodes,
            dedupe_key=node_keys[0],  # survivor
            merge_keys=tuple(node_keys),
            rationale=proposal.rationale,
            rule_ids=proposal.rule_ids,
        )
        return (step,)

    # ------------------------------------------------------------------
    # delete
    # ------------------------------------------------------------------

    def _build_delete(self, proposal: ProposalPacket) -> tuple[DiffStep, ...]:
        """Produce delete steps for all targets.

        Ordering convention (referential integrity):
          1. delete_edge steps (relationships) — sorted by dedupe_key.
          2. delete_node steps (nodes)         — sorted by dedupe_key.

        Edges are deleted first because a node delete with Agent-C's typed
        graph layer may implicitly rely on the edge being gone (even though
        Neo4j DETACH DELETE handles this, the explicit ordering keeps the
        DiffPlan self-documenting).

        Args:
            proposal: ProposalPacket with proposal_class == delete.

        Returns:
            Tuple of DiffSteps (edges first, then nodes, each sorted).

        Log events:
            diff_unknown_element_type WARNING — proposal_id, dedupe_key, element_type
        """
        edge_targets = sorted(
            [t for t in proposal.targets if t.element_type == "relationship"],
            key=lambda t: t.dedupe_key,
        )
        node_targets = sorted(
            [t for t in proposal.targets if t.element_type == "node"],
            key=lambda t: t.dedupe_key,
        )

        steps: list[DiffStep] = []

        for target in edge_targets:
            steps.append(
                DiffStep(
                    operation=DiffOperation.delete_edge,
                    dedupe_key=target.dedupe_key,
                    rationale=proposal.rationale,
                    rule_ids=proposal.rule_ids,
                )
            )

        for target in node_targets:
            steps.append(
                DiffStep(
                    operation=DiffOperation.delete_node,
                    dedupe_key=target.dedupe_key,
                    rationale=proposal.rationale,
                    rule_ids=proposal.rule_ids,
                )
            )

        # Warn about any targets with an unrecognised element_type.
        known_types = {"node", "relationship"}
        for target in proposal.targets:
            if target.element_type not in known_types:
                logger.warning(
                    "diff_unknown_element_type",
                    proposal_id=proposal.proposal_id,
                    dedupe_key=target.dedupe_key,
                    element_type=target.element_type,
                )

        return tuple(steps)

    # ------------------------------------------------------------------
    # defer
    # ------------------------------------------------------------------

    def _build_defer(self, proposal: ProposalPacket) -> tuple[DiffStep, ...]:
        """A defer proposal acknowledges the candidate but takes no action.

        Returns an empty step tuple.  The DiffPlan will have diff_id derived
        from an empty steps list — deterministic and well-formed.

        Args:
            proposal: ProposalPacket with proposal_class == defer.

        Returns:
            Empty tuple.
        """
        return ()
