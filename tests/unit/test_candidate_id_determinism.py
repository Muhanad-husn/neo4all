"""
tests/unit/test_candidate_id_determinism.py — Candidate.candidate_id determinism (SPEC-05 file 7f).

No network, no LLM, no Neo4j.  Tests are purely algebraic, verifying the
identity contract from CLAUDE.md §5:

    candidate_id = SHA-256(canonical_json({
        "run_id": ...,
        "schema_version": ...,
        "candidate_type": ...,
        "involved_element_refs": sorted(...),
        "detection_method": ...
    }))

Coverage:
  - Same inputs on two separate Candidate instances → same candidate_id
  - Order of involved_element_refs does not affect candidate_id (sorted internally)
  - Different severity → same candidate_id (severity excluded from identity)
  - Different collision_context → same candidate_id (context excluded from identity)
  - Different run_id → different candidate_id
  - Different schema_version → different candidate_id
  - Different candidate_type → different candidate_id
  - Different involved_element_refs (superset) → different candidate_id
  - Different detection_method → different candidate_id
  - candidate_id is a 64-char lowercase hex SHA-256 digest
  - Fail-closed: empty run_id raises ValidationError
  - Fail-closed: whitespace-only schema_version raises ValidationError
  - Fail-closed: empty detection_method raises ValidationError
  - Fail-closed: empty involved_element_refs tuple raises ValidationError

Cross-validation via independent reference implementation (mirrors Candidate.candidate_id
but implemented independently in this test file to detect contract drift).
"""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from api.models.candidate import Candidate, CandidateLane, CandidateType, Severity

# ---------------------------------------------------------------------------
# Reference implementation (independent of production code)
# ---------------------------------------------------------------------------


def _reference_candidate_id(
    run_id: str,
    schema_version: str,
    candidate_type: str,
    involved_element_refs: tuple[str, ...],
    detection_method: str,
) -> str:
    """Independent re-implementation of the candidate_id formula from CLAUDE.md §5."""
    identity = {
        "run_id": run_id,
        "schema_version": schema_version,
        "candidate_type": candidate_type,
        "involved_element_refs": sorted(involved_element_refs),
        "detection_method": detection_method,
    }
    serialized = json.dumps(
        identity,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Builder helper — sensible defaults, overrideable via kwargs
# ---------------------------------------------------------------------------

_BASE = dict(
    run_id="unit-test-run-det-0001",
    schema_version="sv_fixture_abc123def456",
    candidate_type=CandidateType.exact_node_duplicate,
    candidate_lane=CandidateLane.node,
    involved_element_refs=("ref_alpha", "ref_beta"),
    severity=Severity.high,
    detection_method="exact_node_dedupe_key",
    collision_context={},
)


def _make(**overrides) -> Candidate:
    return Candidate(**{**_BASE, **overrides})


# ---------------------------------------------------------------------------
# Tests — basic determinism
# ---------------------------------------------------------------------------


def test_same_inputs_same_id() -> None:
    """Two Candidate instances with identical fields produce the same candidate_id."""
    a = _make()
    b = _make()
    assert a.candidate_id == b.candidate_id


def test_candidate_id_format() -> None:
    """candidate_id is a 64-character lowercase hexadecimal SHA-256 digest."""
    cid = _make().candidate_id
    assert len(cid) == 64
    assert cid == cid.lower()
    assert all(c in "0123456789abcdef" for c in cid)


def test_candidate_id_matches_reference_implementation() -> None:
    """candidate_id matches the independently written reference formula."""
    c = _make()
    expected = _reference_candidate_id(
        run_id=c.run_id,
        schema_version=c.schema_version,
        candidate_type=str(c.candidate_type),
        involved_element_refs=c.involved_element_refs,
        detection_method=c.detection_method,
    )
    assert c.candidate_id == expected


# ---------------------------------------------------------------------------
# Tests — ref ordering invariance
# ---------------------------------------------------------------------------


def test_involved_refs_order_independent() -> None:
    """involved_element_refs in different orders → same candidate_id (sorted internally)."""
    a = _make(involved_element_refs=("ref_alpha", "ref_beta"))
    b = _make(involved_element_refs=("ref_beta", "ref_alpha"))
    assert a.candidate_id == b.candidate_id


def test_involved_refs_three_elements_order_independent() -> None:
    """Three refs in every permutation yield the same candidate_id."""
    base_refs = ("ref_1", "ref_2", "ref_3")
    permutations = [
        ("ref_1", "ref_2", "ref_3"),
        ("ref_1", "ref_3", "ref_2"),
        ("ref_2", "ref_1", "ref_3"),
        ("ref_2", "ref_3", "ref_1"),
        ("ref_3", "ref_1", "ref_2"),
        ("ref_3", "ref_2", "ref_1"),
    ]
    ids = {_make(involved_element_refs=p).candidate_id for p in permutations}
    assert len(ids) == 1, f"Got {len(ids)} distinct IDs from permutations — not order-independent"

    # All must match reference
    expected = _reference_candidate_id(
        run_id=_BASE["run_id"],
        schema_version=_BASE["schema_version"],
        candidate_type=str(_BASE["candidate_type"]),
        involved_element_refs=base_refs,
        detection_method=_BASE["detection_method"],
    )
    assert ids.pop() == expected


# ---------------------------------------------------------------------------
# Tests — excluded fields do not affect candidate_id
# ---------------------------------------------------------------------------


def test_different_severity_same_id() -> None:
    """severity is excluded from candidate_id — different values produce same id."""
    a = _make(severity=Severity.critical)
    b = _make(severity=Severity.low)
    assert a.candidate_id == b.candidate_id


def test_different_collision_context_same_id() -> None:
    """collision_context is excluded from candidate_id — different values produce same id."""
    a = _make(collision_context={"score": 0.99, "detail": "verbose"})
    b = _make(collision_context={})
    assert a.candidate_id == b.candidate_id


def test_different_candidate_lane_same_id() -> None:
    """candidate_lane is excluded from candidate_id."""
    a = _make(candidate_lane=CandidateLane.node)
    b = _make(candidate_lane=CandidateLane.structural)
    assert a.candidate_id == b.candidate_id


# ---------------------------------------------------------------------------
# Tests — included fields DO affect candidate_id
# ---------------------------------------------------------------------------


def test_different_run_id_different_id() -> None:
    """Different run_id → different candidate_id."""
    a = _make(run_id="run-aaa-001")
    b = _make(run_id="run-bbb-002")
    assert a.candidate_id != b.candidate_id


def test_different_schema_version_different_id() -> None:
    """Different schema_version → different candidate_id."""
    a = _make(schema_version="sv_abc")
    b = _make(schema_version="sv_xyz")
    assert a.candidate_id != b.candidate_id


def test_different_candidate_type_different_id() -> None:
    """Different candidate_type → different candidate_id."""
    a = _make(candidate_type=CandidateType.exact_node_duplicate)
    b = _make(candidate_type=CandidateType.probable_duplicate)
    assert a.candidate_id != b.candidate_id


def test_different_involved_refs_different_id() -> None:
    """Adding an extra ref to involved_element_refs → different candidate_id."""
    a = _make(involved_element_refs=("ref_alpha",))
    b = _make(involved_element_refs=("ref_alpha", "ref_extra"))
    assert a.candidate_id != b.candidate_id


def test_different_detection_method_different_id() -> None:
    """Different detection_method string → different candidate_id."""
    a = _make(detection_method="method_a")
    b = _make(detection_method="method_b")
    assert a.candidate_id != b.candidate_id


# ---------------------------------------------------------------------------
# Tests — all five detector types produce stable IDs
# ---------------------------------------------------------------------------


def test_all_candidate_types_deterministic() -> None:
    """Every CandidateType produces a stable, type-specific candidate_id."""
    type_lane = {
        CandidateType.exact_node_duplicate: CandidateLane.node,
        CandidateType.exact_rel_duplicate: CandidateLane.relationship,
        CandidateType.probable_duplicate: CandidateLane.node,
        CandidateType.canonical_violation: CandidateLane.relationship,
        CandidateType.structural_anomaly: CandidateLane.structural,
    }
    ids = set()
    for ctype, lane in type_lane.items():
        c1 = _make(candidate_type=ctype, candidate_lane=lane)
        c2 = _make(candidate_type=ctype, candidate_lane=lane)
        assert c1.candidate_id == c2.candidate_id, f"Non-deterministic ID for {ctype}"
        ids.add(c1.candidate_id)
    # All five types produce distinct IDs
    assert len(ids) == 5, "Two different candidate types produced the same candidate_id"


# ---------------------------------------------------------------------------
# Tests — idempotency across process-equivalent calls
# ---------------------------------------------------------------------------


def test_idempotent_from_detector_output() -> None:
    """Re-constructing a Candidate from its own field values yields the same candidate_id."""
    original = _make()
    reconstructed = Candidate(
        run_id=original.run_id,
        schema_version=original.schema_version,
        candidate_type=original.candidate_type,
        candidate_lane=original.candidate_lane,
        involved_element_refs=original.involved_element_refs,
        severity=original.severity,
        detection_method=original.detection_method,
        collision_context=original.collision_context,
    )
    assert original.candidate_id == reconstructed.candidate_id


# ---------------------------------------------------------------------------
# Tests — fail-closed: invalid inputs raise ValidationError
# ---------------------------------------------------------------------------


def test_fail_closed_empty_run_id() -> None:
    """Empty run_id raises ValidationError — model rejects blank identity fields."""
    with pytest.raises(ValidationError):
        _make(run_id="")


def test_fail_closed_whitespace_run_id() -> None:
    """Whitespace-only run_id raises ValidationError."""
    with pytest.raises(ValidationError):
        _make(run_id="   ")


def test_fail_closed_empty_schema_version() -> None:
    """Empty schema_version raises ValidationError."""
    with pytest.raises(ValidationError):
        _make(schema_version="")


def test_fail_closed_empty_detection_method() -> None:
    """Empty detection_method raises ValidationError."""
    with pytest.raises(ValidationError):
        _make(detection_method="")


def test_fail_closed_whitespace_detection_method() -> None:
    """Whitespace-only detection_method raises ValidationError."""
    with pytest.raises(ValidationError):
        _make(detection_method="  ")


def test_fail_closed_empty_involved_element_refs() -> None:
    """Empty involved_element_refs tuple raises ValidationError (at least one ref required)."""
    with pytest.raises(ValidationError):
        _make(involved_element_refs=())
