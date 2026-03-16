"""
api/services/curation/verdict.py — Verdict generalization service (Tier 1).

After a user approves and executes a correction (e.g. normalizing MENTIONS
between Document->Location to LOCATED_IN), this service finds sibling
candidates sharing the same structural pattern and creates batch proposals
replicating the same verdict.

All logic is deterministic (no LLM).  Batch proposals are created in
``pending`` state -- the user must still approve via the existing pipeline.

Public API:
    extract_violation_pattern()   -- extract the (rel_type, start, end, method) tuple
    find_similar_candidates()     -- discover sibling candidates
    create_batch_proposals()      -- replicate a verdict across siblings
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from api.observability.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Eligible proposal classes for verdict generalization (string values to
# avoid importing ProposalClass at module level).
# ---------------------------------------------------------------------------

_ELIGIBLE_CLASSES: frozenset[str] = frozenset({
    "normalize", "canonicalize", "rename",
})


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class VerdictSimilarityResult(BaseModel):
    """Result of find_similar_candidates()."""

    source_proposal_id: str
    source_proposal_class: str
    pattern_key: str  # display-friendly description
    matches: list[Any]  # list[CandidateOut] — Any to avoid import
    total_matches: int


class BatchResult(BaseModel):
    """Result of create_batch_proposals()."""

    proposals_created: int
    proposal_ids: list[str]
    skipped: int
    failed: int


# ---------------------------------------------------------------------------
# Pattern extraction
# ---------------------------------------------------------------------------


def extract_violation_pattern(
    collision_context: dict[str, Any],
    detection_method: str,
) -> tuple[str, str, str, str] | None:
    """Extract the structural pattern key from a canonical_violation candidate.

    Returns ``(rel_type, actual_start_type, actual_end_type, detection_method)``
    for canonical_violation candidates whose detection_method starts with
    ``canonical_``.  Returns ``None`` for other candidate types.

    The collision_context is expected to carry:
      - rel_type (str)
      - actual_start_type (str)
      - actual_end_type (str)
    """
    if not detection_method.startswith("canonical_"):
        return None

    rel_type = collision_context.get("rel_type")
    actual_start = collision_context.get("actual_start_type")
    actual_end = collision_context.get("actual_end_type")

    if not all((rel_type, actual_start, actual_end)):
        return None

    return (str(rel_type), str(actual_start), str(actual_end), detection_method)


# ---------------------------------------------------------------------------
# Find similar candidates
# ---------------------------------------------------------------------------


async def find_similar_candidates(
    run_id: str,
    source_proposal_id: str,
) -> VerdictSimilarityResult | None:
    """Find sibling candidates sharing the same structural violation pattern.

    Steps:
    1. Load source proposal; validate state == executed and class is eligible.
    2. Load schema to get version_hash for cache key computation.
    3. Find source candidate in cached candidates.
    4. Extract pattern key from collision_context.
    5. Filter cached candidates: same candidate_type == canonical_violation,
       same pattern key, exclude source candidate.
    6. Exclude candidates that already have a proposal.
    7. Return matches.

    Returns None if source proposal not found.
    Raises ValueError if source proposal is not in executed state or
    has ineligible proposal_class.
    """
    from api.cache.client import get_cache_client
    from api.cache.keys import CacheKey
    from api.proposals.models import ProposalState
    from api.proposals.service import ProposalService
    from api.routers.candidates import CandidateOut, load_all_cached_candidates
    from api.schema.models import SchemaVersion

    service = ProposalService()

    # 1. Load source proposal
    source = await service.get_for_run(run_id, source_proposal_id)
    if source is None:
        return None

    if source.state != ProposalState.executed:
        raise ValueError(
            f"Source proposal '{source_proposal_id}' is in state "
            f"'{source.state}', expected 'executed'."
        )

    proposal_class = source.proposal_class
    if str(proposal_class) not in _ELIGIBLE_CLASSES:
        raise ValueError(
            f"Source proposal class '{proposal_class}' is not eligible "
            f"for verdict generalization. Eligible: {sorted(_ELIGIBLE_CLASSES)}"
        )

    # 2. Load schema
    cache = get_cache_client()
    schema: SchemaVersion | None = await cache.get(
        CacheKey.schema(run_id=run_id), model=SchemaVersion
    )
    if schema is None:
        logger.warning("verdict_schema_not_found", run_id=run_id)
        return VerdictSimilarityResult(
            source_proposal_id=source_proposal_id,
            source_proposal_class=str(proposal_class),
            pattern_key="",
            matches=[],
            total_matches=0,
        )

    # 3. Load all cached candidates
    all_candidates = await load_all_cached_candidates(run_id, schema.version_hash)

    # Find source candidate
    source_candidate: CandidateOut | None = None
    for c in all_candidates:
        if c.candidate_id == source.candidate_id:
            source_candidate = c
            break

    if source_candidate is None:
        logger.warning(
            "verdict_source_candidate_not_found",
            run_id=run_id,
            candidate_id=source.candidate_id,
        )
        return VerdictSimilarityResult(
            source_proposal_id=source_proposal_id,
            source_proposal_class=str(proposal_class),
            pattern_key="",
            matches=[],
            total_matches=0,
        )

    # 4. Extract pattern key
    pattern = extract_violation_pattern(
        source_candidate.collision_context,
        source_candidate.detection_method,
    )
    if pattern is None:
        logger.info(
            "verdict_no_pattern_extracted",
            run_id=run_id,
            candidate_id=source.candidate_id,
            detection_method=source_candidate.detection_method,
        )
        return VerdictSimilarityResult(
            source_proposal_id=source_proposal_id,
            source_proposal_class=str(proposal_class),
            pattern_key="",
            matches=[],
            total_matches=0,
        )

    pattern_display = f"{pattern[0]}({pattern[1]}->{pattern[2]}) [{pattern[3]}]"

    # 5. Filter candidates: same type + same pattern, exclude source
    siblings: list[CandidateOut] = []
    for c in all_candidates:
        if c.candidate_id == source.candidate_id:
            continue
        if c.candidate_type != "canonical_violation":
            continue
        c_pattern = extract_violation_pattern(
            c.collision_context, c.detection_method,
        )
        if c_pattern == pattern:
            siblings.append(c)

    # 6. Exclude candidates that already have a proposal
    existing_proposals = await service.list_for_run(run_id)
    existing_candidate_ids: set[str] = {p.candidate_id for p in existing_proposals}

    matches = [
        c for c in siblings
        if c.candidate_id not in existing_candidate_ids
    ]

    logger.info(
        "verdict_similar_found",
        run_id=run_id,
        source_proposal_id=source_proposal_id,
        pattern_key=pattern_display,
        total_siblings=len(siblings),
        after_filter=len(matches),
    )

    return VerdictSimilarityResult(
        source_proposal_id=source_proposal_id,
        source_proposal_class=str(proposal_class),
        pattern_key=pattern_display,
        matches=matches,
        total_matches=len(matches),
    )


# ---------------------------------------------------------------------------
# Create batch proposals
# ---------------------------------------------------------------------------


async def create_batch_proposals(
    run_id: str,
    source_proposal_id: str,
    candidate_ids: list[str],
) -> BatchResult:
    """Create proposals for multiple candidates replicating a source verdict.

    Steps:
    1. Load source proposal (validated as executed).
    2. Load schema for schema_version.
    3. Load all cached candidates.
    4. For each candidate_id, build a ProposalPacket using source as template.
    5. Create via ProposalService.create() (idempotent on proposal_id).

    Returns BatchResult with counts.
    """
    from api.cache.client import get_cache_client
    from api.cache.keys import CacheKey
    from api.proposals.models import ElementRef, ProposalPacket, ProposalState
    from api.proposals.service import ProposalService
    from api.routers.candidates import CandidateOut, load_all_cached_candidates
    from api.schema.models import SchemaVersion

    service = ProposalService()

    # 1. Load and validate source proposal
    source = await service.get_for_run(run_id, source_proposal_id)
    if source is None:
        raise ValueError(f"Source proposal '{source_proposal_id}' not found.")

    if source.state != ProposalState.executed:
        raise ValueError(
            f"Source proposal '{source_proposal_id}' is in state "
            f"'{source.state}', expected 'executed'."
        )

    # 2. Load schema
    cache = get_cache_client()
    schema: SchemaVersion | None = await cache.get(
        CacheKey.schema(run_id=run_id), model=SchemaVersion
    )
    if schema is None:
        raise ValueError(f"No locked schema found for run '{run_id}'.")

    # 3. Load all cached candidates and index by candidate_id
    all_candidates = await load_all_cached_candidates(run_id, schema.version_hash)
    candidate_map: dict[str, CandidateOut] = {c.candidate_id: c for c in all_candidates}

    # 4. Build and create proposals
    created_ids: list[str] = []
    skipped = 0
    failed = 0

    # Truncate source rationale for inclusion in batch rationale
    source_rationale_snippet = source.rationale[:100]
    source_id_snippet = source_proposal_id[:16]

    for cid in candidate_ids:
        candidate = candidate_map.get(cid)
        if candidate is None:
            logger.warning(
                "verdict_batch_candidate_not_found",
                run_id=run_id,
                candidate_id=cid,
            )
            failed += 1
            continue

        # Build targets from candidate's involved_element_refs
        # Same construction as _build_proposal_packet in api/agents/proposal.py
        rel_dk: str | None = None
        if candidate.candidate_lane == "relationship":
            rel_dk = candidate.collision_context.get("rel_dedupe_key") or (
                candidate.collision_context.get("dedupe_key")
            )

        targets_list: list[ElementRef] = []
        for ref in candidate.involved_element_refs:
            if rel_dk and ref == rel_dk:
                element_type = "relationship"
            elif candidate.candidate_lane == "relationship":
                element_type = "node"
            else:
                element_type = "node"
            targets_list.append(
                ElementRef(
                    element_type=element_type,
                    dedupe_key=ref,
                    label=ref,
                )
            )

        packet = ProposalPacket(
            run_id=run_id,
            candidate_id=cid,
            proposal_class=source.proposal_class,
            schema_version=schema.version_hash,
            evidence_chunk_ids=source.evidence_chunk_ids,
            evidence_doc_ids=source.evidence_doc_ids,
            targets=tuple(targets_list),
            rule_ids=source.rule_ids,
            rationale=(
                f"Verdict generalization: same correction as "
                f"{source_id_snippet}... -- {source_rationale_snippet}"
            ),
            confidence_score=1.0,
            high_risk_override=source.high_risk_override,
        )

        try:
            stored = await service.create(packet)
            created_ids.append(stored.proposal_id)
        except Exception as exc:
            logger.warning(
                "verdict_batch_create_failed",
                run_id=run_id,
                candidate_id=cid,
                error=str(exc),
            )
            failed += 1

    logger.info(
        "verdict_batch_complete",
        run_id=run_id,
        source_proposal_id=source_proposal_id,
        created=len(created_ids),
        skipped=skipped,
        failed=failed,
    )

    return BatchResult(
        proposals_created=len(created_ids),
        proposal_ids=created_ids,
        skipped=skipped,
        failed=failed,
    )
