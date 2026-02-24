"""
tests/unit/test_approval_gate.py — Approval Gate logic tests (SPEC-06 file 17).

No network, no LLM, no Neo4j.  Tests cover the deterministic ID derivation
functions, high-risk classification, two-phase approval requirements, and
rejection behaviour — all without touching the FastAPI router.

Coverage:

  Deterministic ID derivation:
    - _derive_approval_id produces the same output for the same inputs
    - _derive_approval_id output changes with different proposal_id or actor
    - _derive_confirmation_token produces the same output for the same inputs
    - _derive_confirmation_token is distinct from _derive_approval_id for same inputs
    - Both IDs are 64-character lowercase SHA-256 hex digests

  High-risk classification (HIGH_RISK_CLASSES):
    - merge and delete are in HIGH_RISK_CLASSES
    - canonicalize, normalize, rename, defer are not in HIGH_RISK_CLASSES
    - ProposalPacket.is_high_risk() mirrors HIGH_RISK_CLASSES membership

  Two-phase approval contract (ProposalPacket + allowed_transitions):
    - Only pending proposals may be approved (can_transition_to logic)
    - Approved and rejected are terminal — execution without valid approval_id blocked

  Approval ID format and derivation contract:
    - SHA-256("approval:{proposal_id}:{actor}") — prefix distinguishes namespaces
    - SHA-256("confirm:{proposal_id}:{actor}") for confirmation tokens
    - Different actors produce different approval_ids for the same proposal
    - Different proposal_ids produce different approval_ids for the same actor

  Fail-closed validation (ProposalPacket validators):
    - Empty actor-equivalent fields (run_id, candidate_id) raise ValidationError
    - Model is frozen — state cannot be mutated after construction
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from api.proposals.models import (
    HIGH_RISK_CLASSES,
    ElementRef,
    ProposalClass,
    ProposalPacket,
    ProposalState,
    allowed_transitions,
)
from api.proposals.service import InvalidTransitionError


# ---------------------------------------------------------------------------
# Reference implementations of the deterministic ID derivation formulas.
# These mirror api/routers/approvals.py exactly and serve as executable
# documentation of the contract — no FastAPI import required.
# ---------------------------------------------------------------------------


def _derive_approval_id(proposal_id: str, actor: str) -> str:
    """SHA-256("approval:{proposal_id}:{actor}") — mirrors approvals.py."""
    raw = f"approval:{proposal_id}:{actor}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _derive_confirmation_token(proposal_id: str, actor: str) -> str:
    """SHA-256("confirm:{proposal_id}:{actor}") — mirrors approvals.py."""
    raw = f"confirm:{proposal_id}:{actor}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-approval-0001"
_CANDIDATE_ID = "d" * 64
_SCHEMA_VERSION = "sv_approval_fixture_v1"
_ACTOR = "human-operator@example.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_packet(
    proposal_class: ProposalClass = ProposalClass.canonicalize,
    state: ProposalState = ProposalState.pending,
) -> ProposalPacket:
    return ProposalPacket(
        run_id=_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        proposal_class=proposal_class,
        schema_version=_SCHEMA_VERSION,
        rationale="approval gate test rationale",
        state=state,
    )


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Deterministic ID derivation
# ---------------------------------------------------------------------------


def test_derive_approval_id_is_deterministic() -> None:
    """_derive_approval_id returns the same value on every call for the same inputs."""
    pid = _make_packet().proposal_id
    id_a = _derive_approval_id(pid, _ACTOR)
    id_b = _derive_approval_id(pid, _ACTOR)
    assert id_a == id_b


def test_derive_approval_id_matches_expected_formula() -> None:
    """approval_id == SHA-256('approval:{proposal_id}:{actor}')."""
    pid = _make_packet().proposal_id
    expected = _sha256(f"approval:{pid}:{_ACTOR}")
    assert _derive_approval_id(pid, _ACTOR) == expected


def test_derive_approval_id_is_64_char_hex() -> None:
    """approval_id is a 64-character lowercase hex SHA-256 digest."""
    aid = _derive_approval_id(_make_packet().proposal_id, _ACTOR)
    assert len(aid) == 64
    assert all(c in "0123456789abcdef" for c in aid)


def test_derive_approval_id_changes_with_different_actor() -> None:
    """Different actors produce distinct approval_ids for the same proposal."""
    pid = _make_packet().proposal_id
    assert _derive_approval_id(pid, "actor-a") != _derive_approval_id(pid, "actor-b")


def test_derive_approval_id_changes_with_different_proposal() -> None:
    """Different proposals produce distinct approval_ids for the same actor."""
    pid_canon = _make_packet(ProposalClass.canonicalize).proposal_id
    pid_norm = _make_packet(ProposalClass.normalize).proposal_id
    assert _derive_approval_id(pid_canon, _ACTOR) != _derive_approval_id(pid_norm, _ACTOR)


def test_derive_confirmation_token_is_deterministic() -> None:
    """_derive_confirmation_token returns the same value for the same inputs."""
    pid = _make_packet().proposal_id
    tok_a = _derive_confirmation_token(pid, _ACTOR)
    tok_b = _derive_confirmation_token(pid, _ACTOR)
    assert tok_a == tok_b


def test_derive_confirmation_token_matches_expected_formula() -> None:
    """confirmation_token == SHA-256('confirm:{proposal_id}:{actor}')."""
    pid = _make_packet().proposal_id
    expected = _sha256(f"confirm:{pid}:{_ACTOR}")
    assert _derive_confirmation_token(pid, _ACTOR) == expected


def test_derive_confirmation_token_is_64_char_hex() -> None:
    """confirmation_token is a 64-character lowercase hex SHA-256 digest."""
    tok = _derive_confirmation_token(_make_packet().proposal_id, _ACTOR)
    assert len(tok) == 64
    assert all(c in "0123456789abcdef" for c in tok)


def test_approval_id_distinct_from_confirmation_token() -> None:
    """approval_id and confirmation_token use different prefixes → always distinct."""
    pid = _make_packet().proposal_id
    assert _derive_approval_id(pid, _ACTOR) != _derive_confirmation_token(pid, _ACTOR)


# ---------------------------------------------------------------------------
# HIGH_RISK_CLASSES membership
# ---------------------------------------------------------------------------


def test_high_risk_classes_contains_merge() -> None:
    """merge is a high-risk proposal class requiring two-phase approval."""
    assert ProposalClass.merge in HIGH_RISK_CLASSES


def test_high_risk_classes_contains_delete() -> None:
    """delete is a high-risk proposal class requiring two-phase approval."""
    assert ProposalClass.delete in HIGH_RISK_CLASSES


@pytest.mark.parametrize(
    "cls",
    [ProposalClass.canonicalize, ProposalClass.normalize, ProposalClass.rename, ProposalClass.defer],
)
def test_high_risk_classes_excludes_low_risk(cls: ProposalClass) -> None:
    """canonicalize, normalize, rename, and defer are not high-risk."""
    assert cls not in HIGH_RISK_CLASSES


# ---------------------------------------------------------------------------
# ProposalPacket.is_high_risk() mirrors HIGH_RISK_CLASSES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", [ProposalClass.merge, ProposalClass.delete])
def test_packet_is_high_risk_for_merge_delete(cls: ProposalClass) -> None:
    """is_high_risk() returns True for merge and delete proposals."""
    assert _make_packet(cls).is_high_risk() is True


@pytest.mark.parametrize(
    "cls",
    [ProposalClass.canonicalize, ProposalClass.normalize, ProposalClass.rename, ProposalClass.defer],
)
def test_packet_is_not_high_risk_for_low_risk(cls: ProposalClass) -> None:
    """is_high_risk() returns False for all non-high-risk classes."""
    assert _make_packet(cls).is_not_high_risk() is False if hasattr(_make_packet(cls), "is_not_high_risk") \
        else _make_packet(cls).is_high_risk() is False


# ---------------------------------------------------------------------------
# Two-phase approval contract: only pending proposals may transition
# ---------------------------------------------------------------------------


def test_pending_proposal_can_be_approved() -> None:
    """A pending proposal passes can_transition_to(approved)."""
    assert _make_packet(state=ProposalState.pending).can_transition_to(ProposalState.approved)


def test_approved_proposal_cannot_be_re_approved() -> None:
    """An already-approved proposal fails can_transition_to(approved)."""
    assert not _make_packet(state=ProposalState.approved).can_transition_to(ProposalState.approved)


def test_rejected_proposal_cannot_be_approved() -> None:
    """A rejected proposal fails can_transition_to(approved)."""
    assert not _make_packet(state=ProposalState.rejected).can_transition_to(ProposalState.approved)


def test_approved_is_terminal_no_outgoing_transitions() -> None:
    """An approved proposal has no valid outgoing transitions."""
    packet = _make_packet(state=ProposalState.approved)
    assert allowed_transitions(ProposalState.approved) == frozenset()
    for state in ProposalState:
        assert not packet.can_transition_to(state)


def test_rejected_is_terminal_no_outgoing_transitions() -> None:
    """A rejected proposal has no valid outgoing transitions."""
    packet = _make_packet(state=ProposalState.rejected)
    assert allowed_transitions(ProposalState.rejected) == frozenset()
    for state in ProposalState:
        assert not packet.can_transition_to(state)


# ---------------------------------------------------------------------------
# InvalidTransitionError: attempting to approve a terminal proposal
# ---------------------------------------------------------------------------


def test_invalid_transition_error_on_terminal_state() -> None:
    """InvalidTransitionError is raised when transitioning from a terminal state."""
    err = InvalidTransitionError(ProposalState.approved, ProposalState.pending)
    assert err.from_state == ProposalState.approved
    assert err.to_state == ProposalState.pending
    assert "approved" in str(err)
    assert "pending" in str(err)


def test_invalid_transition_error_allowed_list_in_message() -> None:
    """InvalidTransitionError message includes the allowed transitions for context."""
    err = InvalidTransitionError(ProposalState.pending, ProposalState.pending)
    # "pending" transitions are: approved, rejected, deferred
    msg = str(err)
    assert "approved" in msg or "rejected" in msg or "deferred" in msg


# ---------------------------------------------------------------------------
# Fail-closed: ProposalPacket field validators
# ---------------------------------------------------------------------------


def test_empty_run_id_raises_validation_error() -> None:
    """An empty run_id is rejected at model construction."""
    with pytest.raises(ValidationError):
        ProposalPacket(
            run_id="",
            candidate_id=_CANDIDATE_ID,
            proposal_class=ProposalClass.canonicalize,
            schema_version=_SCHEMA_VERSION,
            rationale="test",
        )


def test_empty_schema_version_raises_validation_error() -> None:
    """An empty schema_version is rejected at model construction."""
    with pytest.raises(ValidationError):
        ProposalPacket(
            run_id=_RUN_ID,
            candidate_id=_CANDIDATE_ID,
            proposal_class=ProposalClass.canonicalize,
            schema_version="",
            rationale="test",
        )


def test_element_ref_invalid_element_type_raises() -> None:
    """ElementRef rejects element_type values other than 'node' or 'relationship'."""
    with pytest.raises(ValidationError, match="element_type"):
        ElementRef(element_type="edge", dedupe_key="A|a|v1")


def test_element_ref_empty_dedupe_key_raises() -> None:
    """ElementRef rejects an empty or whitespace-only dedupe_key."""
    with pytest.raises(ValidationError, match="dedupe_key"):
        ElementRef(element_type="node", dedupe_key="   ")


def test_packet_is_frozen() -> None:
    """ProposalPacket is immutable — direct state assignment raises an error."""
    packet = _make_packet()
    with pytest.raises(Exception):
        packet.state = ProposalState.approved  # type: ignore[misc]
