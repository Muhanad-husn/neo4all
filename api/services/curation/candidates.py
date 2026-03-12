"""
api/services/curation/candidates.py — Five deterministic candidate detectors (SPEC-05 S-05.1).

Zero-LLM.  Each detector accepts typed graph reader result models (no direct
Neo4j access) and returns list[Candidate] with deterministic candidate_ids.

Detectors
---------
ExactNodeDuplicateDetector   Same NodeType + dedupe_key appears more than once.
ExactRelDuplicateDetector    Same rel dedupe_key (type + start + end) appears more than once.
ProbableDuplicateDetector    Same-type node pairs with Jaro-Winkler >= JW_THRESHOLD.
CanonicalViolationDetector   Direction or inverse violations against schema EdgeTypeDefs.
StructuralAnomalyDetector    Orphan nodes, degree outliers, missing provenance, qualifier gaps.

All candidate_ids are deterministic SHA-256 digests derived by api/models/candidate.py.
"""

from __future__ import annotations

import math
from collections import defaultdict

from api.graph.reader_models import (
    DegreeListResult,
    GraphNodeRecord,
    GraphRelRecord,
    NodeListResult,
    OrphanResult,
    RelListResult,
)
from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity
from api.observability.logger import get_logger
from api.schema.models import SchemaVersion
from api.services.curation.similarity import (
    _build_adjacency,
    _jaccard,
    _jaro_winkler,
    _primary_value,
    _token_overlap,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Thresholds / constants
# ---------------------------------------------------------------------------

JW_THRESHOLD: float = 0.90
"""Jaro-Winkler similarity threshold for probable duplicate detection.

Candidates with JW in [0.90, 0.95) are only emitted when token overlap
>= 0.5 (see ProbableDuplicateDetector).  This filters out noisy
near-matches while still capturing genuine duplicates with structural
corroboration.
"""

JW_SOFT_THRESHOLD: float = 0.95
"""Above this JW score, candidates are emitted unconditionally (no token gate)."""

TOKEN_OVERLAP_GATE: float = 0.5
"""Minimum token overlap required to emit candidates in the [0.90, 0.95) band."""

DEGREE_OUTLIER_SIGMA: float = 3.0
"""Standard-deviation multiplier for flagging degree outliers."""

MIN_NODES_FOR_STATS: int = 3
"""Minimum node count needed to compute meaningful degree statistics."""


# ---------------------------------------------------------------------------
# 1. ExactNodeDuplicateDetector
# ---------------------------------------------------------------------------


class ExactNodeDuplicateDetector:
    """Flag nodes that share (node_type, dedupe_key) — exact graph-level duplicates.

    Arises when the same real-world entity is inserted multiple times without
    the unique constraint enforced (e.g., bulk ingestion with deferred checks).
    One Candidate is emitted per (node_type, dedupe_key) group with count >= 2.
    """

    DETECTION_METHOD = "exact_node_dedupe_key"

    def detect(
        self,
        run_id: str,
        schema_version: str,
        nodes: NodeListResult,
    ) -> list[Candidate]:
        """Return one Candidate per duplicate (node_type, dedupe_key) group."""
        groups: dict[tuple[str, str], int] = defaultdict(int)
        for node in nodes.nodes:
            groups[(node.node_type, node.dedupe_key)] += 1

        candidates: list[Candidate] = []
        for (node_type, dedupe_key), count in groups.items():
            if count < 2:
                continue
            candidates.append(
                Candidate(
                    run_id=run_id,
                    schema_version=schema_version,
                    candidate_type=CandidateType.exact_node_duplicate,
                    candidate_lane=CandidateLane.node,
                    involved_element_refs=(dedupe_key,),
                    severity=Severity.high,
                    detection_method=self.DETECTION_METHOD,
                    collision_context={
                        "node_type": node_type,
                        "dedupe_key": dedupe_key,
                        "duplicate_count": count,
                    },
                )
            )

        logger.info(
            "detector_run_complete",
            detector_name="ExactNodeDuplicateDetector",
            run_id=run_id,
            candidates_found=len(candidates),
        )
        return candidates


# ---------------------------------------------------------------------------
# 2. ExactRelDuplicateDetector
# ---------------------------------------------------------------------------


class ExactRelDuplicateDetector:
    """Flag relationships whose dedupe_key appears more than once in the graph.

    A relationship's dedupe_key encodes (RelType, start_key, end_key,
    schema_version) — CLAUDE.md §5 — so the same dedupe_key always means the
    same logical edge.  Count > 1 means a constraint was not enforced at write
    time.  One Candidate is emitted per duplicate dedupe_key.
    """

    DETECTION_METHOD = "exact_rel_dedupe_key"

    def detect(
        self,
        run_id: str,
        schema_version: str,
        rels: RelListResult,
    ) -> list[Candidate]:
        """Return one Candidate per rel dedupe_key that appears more than once."""
        groups: dict[str, list[GraphRelRecord]] = defaultdict(list)
        for rel in rels.rels:
            groups[rel.dedupe_key].append(rel)

        candidates: list[Candidate] = []
        for dedupe_key, members in groups.items():
            if len(members) < 2:
                continue
            rep = members[0]
            candidates.append(
                Candidate(
                    run_id=run_id,
                    schema_version=schema_version,
                    candidate_type=CandidateType.exact_rel_duplicate,
                    candidate_lane=CandidateLane.relationship,
                    involved_element_refs=(dedupe_key,),
                    severity=Severity.high,
                    detection_method=self.DETECTION_METHOD,
                    collision_context={
                        "rel_type": rep.rel_type,
                        "start_dedupe_key": rep.start_dedupe_key,
                        "end_dedupe_key": rep.end_dedupe_key,
                        "dedupe_key": dedupe_key,
                        "duplicate_count": len(members),
                    },
                )
            )

        logger.info(
            "detector_run_complete",
            detector_name="ExactRelDuplicateDetector",
            run_id=run_id,
            candidates_found=len(candidates),
        )
        return candidates


# ---------------------------------------------------------------------------
# 3. ProbableDuplicateDetector
# ---------------------------------------------------------------------------


class ProbableDuplicateDetector:
    """Flag same-type node pairs with Jaro-Winkler similarity >= JW_THRESHOLD.

    Blocking key: node_type (pairs are only formed within the same type).
    Comparison value: primary_property from SchemaVersion (lower-cased str).
    Context score: Jaccard similarity of neighbor sets from RelListResult
                   (stored in collision_context; not used as a filter).

    Requires SchemaVersion to resolve primary_property per node type.
    rels is optional; omitting it sets context_jaccard to 0.0.
    """

    DETECTION_METHOD = f"jaro_winkler_{JW_THRESHOLD:.2f}"

    def detect(
        self,
        run_id: str,
        schema_version: str,
        nodes: NodeListResult,
        schema: SchemaVersion,
        rels: RelListResult | None = None,
    ) -> list[Candidate]:
        """Return Candidates for node pairs with Jaro-Winkler >= JW_THRESHOLD."""
        primary_props: dict[str, str] = {
            ndef.type: ndef.primary_property for ndef in schema.nodes
        }
        by_type: dict[str, list[GraphNodeRecord]] = defaultdict(list)
        for node in nodes.nodes:
            by_type[node.node_type].append(node)

        adj: dict[str, set[str]] = _build_adjacency(rels) if rels is not None else {}

        candidates: list[Candidate] = []
        for node_type, members in by_type.items():
            prop = primary_props.get(node_type)
            if prop is None or len(members) < 2:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    val_a = _primary_value(a, prop)
                    val_b = _primary_value(b, prop)
                    if not val_a or not val_b:
                        continue
                    jw = _jaro_winkler(val_a, val_b)
                    if jw < JW_THRESHOLD:
                        continue
                    tok = _token_overlap(val_a, val_b)
                    # Soft gate: JW in [0.90, 0.95) requires token overlap >= 0.5
                    if jw < JW_SOFT_THRESHOLD and tok < TOKEN_OVERLAP_GATE:
                        continue
                    context_score = _jaccard(
                        adj.get(a.dedupe_key, set()),
                        adj.get(b.dedupe_key, set()),
                    )
                    # Sort refs for order-independent candidate_id (Candidate model
                    # also sorts, but explicit here for clarity).
                    refs = tuple(sorted([a.dedupe_key, b.dedupe_key]))
                    candidates.append(
                        Candidate(
                            run_id=run_id,
                            schema_version=schema_version,
                            candidate_type=CandidateType.probable_duplicate,
                            candidate_lane=CandidateLane.node,
                            involved_element_refs=refs,
                            severity=Severity.medium,
                            detection_method=self.DETECTION_METHOD,
                            collision_context={
                                "node_type": node_type,
                                "primary_property": prop,
                                "value_a": val_a,
                                "value_b": val_b,
                                "jaro_winkler": round(jw, 6),
                                "token_overlap": round(tok, 6),
                                "context_jaccard": round(context_score, 6),
                            },
                        )
                    )

        logger.info(
            "detector_run_complete",
            detector_name="ProbableDuplicateDetector",
            run_id=run_id,
            candidates_found=len(candidates),
        )
        return candidates


# ---------------------------------------------------------------------------
# 4. CanonicalViolationDetector
# ---------------------------------------------------------------------------


class CanonicalViolationDetector:
    """Flag relationships whose direction violates the schema EdgeTypeDefs.

    For each relationship in the graph:
    - Inverse violation: the reversed (end→start) direction matches a valid
      schema edge — the edge exists but was stored backwards.
    - Direction violation: neither forward nor reversed direction matches any
      schema edge for this rel_type — node types are wrong for this rel type.

    Requires NodeListResult to resolve dedupe_key → node_type.
    """

    DETECTION_METHOD_INVERSE = "canonical_inverse_violation"
    DETECTION_METHOD_DIRECTION = "canonical_direction_violation"

    def detect(
        self,
        run_id: str,
        schema_version: str,
        rels: RelListResult,
        nodes: NodeListResult,
        schema: SchemaVersion,
    ) -> list[Candidate]:
        """Return Candidates for every schema-violating relationship."""
        node_type_map: dict[str, str] = {n.dedupe_key: n.node_type for n in nodes.nodes}

        valid_forward: set[tuple[str, str, str]] = {
            (edge.type, edge.start_node_type, edge.end_node_type)
            for edge in schema.edges
        }

        candidates: list[Candidate] = []
        for rel in rels.rels:
            start_type = node_type_map.get(rel.start_dedupe_key, "")
            end_type = node_type_map.get(rel.end_dedupe_key, "")

            if (rel.rel_type, start_type, end_type) in valid_forward:
                continue  # schema-compliant

            if (rel.rel_type, end_type, start_type) in valid_forward:
                # Correct node types, but the edge was stored in reverse direction.
                candidates.append(
                    Candidate(
                        run_id=run_id,
                        schema_version=schema_version,
                        candidate_type=CandidateType.canonical_violation,
                        candidate_lane=CandidateLane.relationship,
                        involved_element_refs=(
                            rel.dedupe_key,
                            rel.start_dedupe_key,
                            rel.end_dedupe_key,
                        ),
                        severity=Severity.high,
                        detection_method=self.DETECTION_METHOD_INVERSE,
                        collision_context={
                            "rel_type": rel.rel_type,
                            "actual_start_type": start_type,
                            "actual_end_type": end_type,
                            "expected_start_type": end_type,
                            "expected_end_type": start_type,
                            "rel_dedupe_key": rel.dedupe_key,
                        },
                    )
                )
            else:
                # The start/end node types do not match any schema definition.
                candidates.append(
                    Candidate(
                        run_id=run_id,
                        schema_version=schema_version,
                        candidate_type=CandidateType.canonical_violation,
                        candidate_lane=CandidateLane.relationship,
                        involved_element_refs=(rel.dedupe_key,),
                        severity=Severity.medium,
                        detection_method=self.DETECTION_METHOD_DIRECTION,
                        collision_context={
                            "rel_type": rel.rel_type,
                            "actual_start_type": start_type,
                            "actual_end_type": end_type,
                            "rel_dedupe_key": rel.dedupe_key,
                        },
                    )
                )

        logger.info(
            "detector_run_complete",
            detector_name="CanonicalViolationDetector",
            run_id=run_id,
            candidates_found=len(candidates),
        )
        return candidates


# ---------------------------------------------------------------------------
# 5. StructuralAnomalyDetector
# ---------------------------------------------------------------------------


class StructuralAnomalyDetector:
    """Flag structural graph anomalies across four sub-detectors.

    Sub-detectors (all deterministic, zero-LLM):
        orphan_node            Degree-zero nodes (no edges in or out).
        degree_outlier         Total degree > mean + DEGREE_OUTLIER_SIGMA * stddev.
        missing_provenance     Node or rel with blank schema_version field.
        qualifier_missing      Node whose type defines a qualifier property that
                               is absent or empty in the node's stored properties.

    schema is required only for qualifier_missing detection.
    """

    def detect(
        self,
        run_id: str,
        schema_version: str,
        nodes: NodeListResult,
        rels: RelListResult,
        orphans: OrphanResult,
        degrees: DegreeListResult,
        schema: SchemaVersion | None = None,
    ) -> list[Candidate]:
        """Run all sub-detectors and return the merged candidate list."""
        candidates: list[Candidate] = []
        candidates.extend(self._orphan_candidates(run_id, schema_version, orphans))
        candidates.extend(self._degree_outlier_candidates(run_id, schema_version, degrees))
        candidates.extend(self._provenance_candidates(run_id, schema_version, nodes, rels))
        if schema is not None:
            candidates.extend(self._qualifier_candidates(run_id, schema_version, nodes, schema))

        logger.info(
            "detector_run_complete",
            detector_name="StructuralAnomalyDetector",
            run_id=run_id,
            candidates_found=len(candidates),
        )
        return candidates

    # ------------------------------------------------------------------
    # Sub-detectors
    # ------------------------------------------------------------------

    @staticmethod
    def _orphan_candidates(
        run_id: str,
        schema_version: str,
        orphans: OrphanResult,
    ) -> list[Candidate]:
        return [
            Candidate(
                run_id=run_id,
                schema_version=schema_version,
                candidate_type=CandidateType.structural_anomaly,
                candidate_lane=CandidateLane.structural,
                involved_element_refs=(dedupe_key,),
                severity=Severity.medium,
                detection_method="orphan_node",
                collision_context={"dedupe_key": dedupe_key},
            )
            for dedupe_key in orphans.orphan_dedupe_keys
        ]

    @staticmethod
    def _degree_outlier_candidates(
        run_id: str,
        schema_version: str,
        degrees: DegreeListResult,
    ) -> list[Candidate]:
        if len(degrees.degrees) < MIN_NODES_FOR_STATS:
            return []
        totals = [r.in_degree + r.out_degree for r in degrees.degrees]
        mean = sum(totals) / len(totals)
        variance = sum((d - mean) ** 2 for d in totals) / len(totals)
        std_dev = math.sqrt(variance)
        if std_dev == 0.0:
            return []
        threshold = mean + DEGREE_OUTLIER_SIGMA * std_dev
        return [
            Candidate(
                run_id=run_id,
                schema_version=schema_version,
                candidate_type=CandidateType.structural_anomaly,
                candidate_lane=CandidateLane.structural,
                involved_element_refs=(rec.dedupe_key,),
                severity=Severity.low,
                detection_method="degree_outlier",
                collision_context={
                    "dedupe_key": rec.dedupe_key,
                    "total_degree": total,
                    "mean_degree": round(mean, 4),
                    "std_dev": round(std_dev, 4),
                    "threshold": round(threshold, 4),
                },
            )
            for rec, total in zip(degrees.degrees, totals)
            if total > threshold
        ]

    @staticmethod
    def _provenance_candidates(
        run_id: str,
        schema_version: str,
        nodes: NodeListResult,
        rels: RelListResult,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for node in nodes.nodes:
            if not node.schema_version.strip():
                candidates.append(
                    Candidate(
                        run_id=run_id,
                        schema_version=schema_version,
                        candidate_type=CandidateType.structural_anomaly,
                        candidate_lane=CandidateLane.structural,
                        involved_element_refs=(node.dedupe_key,),
                        severity=Severity.critical,
                        detection_method="missing_provenance_node",
                        collision_context={
                            "dedupe_key": node.dedupe_key,
                            "node_type": node.node_type,
                            "missing_field": "schema_version",
                        },
                    )
                )
        for rel in rels.rels:
            if not rel.schema_version.strip():
                candidates.append(
                    Candidate(
                        run_id=run_id,
                        schema_version=schema_version,
                        candidate_type=CandidateType.structural_anomaly,
                        candidate_lane=CandidateLane.structural,
                        involved_element_refs=(rel.dedupe_key,),
                        severity=Severity.critical,
                        detection_method="missing_provenance_rel",
                        collision_context={
                            "dedupe_key": rel.dedupe_key,
                            "rel_type": rel.rel_type,
                            "missing_field": "schema_version",
                        },
                    )
                )
        return candidates

    @staticmethod
    def _qualifier_candidates(
        run_id: str,
        schema_version: str,
        nodes: NodeListResult,
        schema: SchemaVersion,
    ) -> list[Candidate]:
        """Flag nodes whose type defines a qualifier property that is absent or empty."""
        qualifier_map: dict[str, str] = {
            ndef.type: ndef.qualifier
            for ndef in schema.nodes
            if ndef.qualifier is not None
        }
        if not qualifier_map:
            return []
        candidates: list[Candidate] = []
        for node in nodes.nodes:
            qualifier_prop = qualifier_map.get(node.node_type)
            if qualifier_prop is None:
                continue
            value = node.properties.get(qualifier_prop)
            if value is None or (isinstance(value, str) and not value.strip()):
                candidates.append(
                    Candidate(
                        run_id=run_id,
                        schema_version=schema_version,
                        candidate_type=CandidateType.structural_anomaly,
                        candidate_lane=CandidateLane.structural,
                        involved_element_refs=(node.dedupe_key,),
                        severity=Severity.medium,
                        detection_method="qualifier_missing",
                        collision_context={
                            "dedupe_key": node.dedupe_key,
                            "node_type": node.node_type,
                            "qualifier_property": qualifier_prop,
                        },
                    )
                )
        return candidates
