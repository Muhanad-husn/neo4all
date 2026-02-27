"""
tests/unit/test_proposal_lifecycle.py — ProposalPacket and ProposalService lifecycle
tests (SPEC-06 file 16).

No network, no LLM, no Neo4j.  S3 and Redis are replaced by in-memory stubs
injected via constructor parameters and unittest.mock.patch.

Coverage:

  Model-level (ProposalPacket):
    - Valid construction and computed proposal_id
    - proposal_id is a deterministic 64-char SHA-256 digest
    - proposal_id is excluded from identity when evidence/state changes
    - Empty rationale raises ValidationError (fail-closed)
    - Candidate ID wrong length raises ValidationError
    - Whitespace-only run_id raises ValidationError
    - is_high_risk() returns True for merge/delete only
    - can_transition_to() valid and invalid transitions

  State machine (allowed_transitions + ProposalPacket.can_transition_to):
    - pending → approved / rejected / deferred (all valid)
    - deferred → pending (re-open valid)
    - approved is terminal (no outgoing transitions)
    - rejected is terminal (no outgoing transitions)
    - InvalidTransitionError carries from_state and to_state

  Service-level (ProposalService with fake S3 + fake cache):
    - create() persists packet and returns it
    - create() is idempotent for the same proposal_id
    - transition() updates state and returns new packet
    - transition() on missing proposal raises ProposalNotFoundError
    - transition() on invalid move raises InvalidTransitionError
    - deferred → pending re-open via transition()
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from api.proposals.models import (
    ElementRef,
    ProposalClass,
    ProposalPacket,
    ProposalState,
    allowed_transitions,
)
from api.proposals.service import (
    InvalidTransitionError,
    ProposalNotFoundError,
    ProposalService,
    ProposalValidationError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-lifecycle-0001"
_CANDIDATE_ID = "b" * 64
_SCHEMA_VERSION = "sv_lifecycle_fixture_v1"

_FAKE_SETTINGS = SimpleNamespace(
    S3_BUCKET_NAME="test-bucket",
    S3_ENDPOINT_URL="http://localhost:9000",
    S3_ACCESS_KEY_ID="test-key",
    S3_SECRET_ACCESS_KEY="test-secret",
)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _FakeS3:
    """In-memory S3 stub — supports put_object and get_object."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "") -> dict:
        self._store[Key] = Body
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict:
        if Key not in self._store:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self._store[Key])}


class _FakeCache:
    """In-memory cache stub implementing the CacheClient interface used by ProposalService."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def get(self, key: str, model: Any = None) -> Any:
        raw = self._store.get(key)
        if raw is None:
            return None
        if model is not None and isinstance(raw, dict):
            return model.model_validate(raw)
        return raw

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        if hasattr(value, "model_dump"):
            self._store[key] = value.model_dump(mode="json")
        else:
            self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


def _make_packet(**kwargs: Any) -> ProposalPacket:
    defaults: dict[str, Any] = dict(
        run_id=_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        proposal_class=ProposalClass.canonicalize,
        schema_version=_SCHEMA_VERSION,
        rationale="test lifecycle rationale",
    )
    defaults.update(kwargs)
    return ProposalPacket(**defaults)


def _make_service(fake_cache: _FakeCache | None = None) -> tuple[ProposalService, _FakeS3]:
    s3 = _FakeS3()
    cache = fake_cache or _FakeCache()
    with patch("api.proposals.service.get_cache_client", return_value=cache):
        svc = ProposalService(settings=_FAKE_SETTINGS, s3_client=s3)
    return svc, s3


# ---------------------------------------------------------------------------
# ProposalPacket model-level tests
# ---------------------------------------------------------------------------


def test_packet_valid_construction() -> None:
    """A fully specified ProposalPacket constructs without errors."""
    packet = _make_packet()
    assert packet.run_id == _RUN_ID
    assert packet.state == ProposalState.pending


def test_proposal_id_is_deterministic() -> None:
    """Same linkage fields always produce the same proposal_id."""
    p1 = _make_packet()
    p2 = _make_packet()
    assert p1.proposal_id == p2.proposal_id


def test_proposal_id_is_64_char_hex() -> None:
    """proposal_id is a 64-character lowercase SHA-256 hex digest."""
    pid = _make_packet().proposal_id
    assert len(pid) == 64
    assert all(c in "0123456789abcdef" for c in pid)


def test_proposal_id_excludes_evidence_fields() -> None:
    """Evidence and governance fields do not affect proposal_id."""
    p_no_evidence = _make_packet()
    p_with_evidence = _make_packet(
        evidence_chunk_ids=("ck1", "ck2"),
        evidence_doc_ids=("d1",),
        rule_ids=("R-01",),
        rationale="different rationale",
        confidence_score=0.5,
    )
    assert p_no_evidence.proposal_id == p_with_evidence.proposal_id


def test_proposal_id_changes_with_proposal_class() -> None:
    """Changing proposal_class produces a different proposal_id."""
    p_canon = _make_packet(proposal_class=ProposalClass.canonicalize)
    p_delete = _make_packet(proposal_class=ProposalClass.delete)
    assert p_canon.proposal_id != p_delete.proposal_id


def test_empty_rationale_raises_validation_error() -> None:
    """An empty rationale is rejected at model construction (fail-closed)."""
    with pytest.raises(ValidationError, match="rationale"):
        _make_packet(rationale="")


def test_whitespace_only_rationale_raises_validation_error() -> None:
    """A whitespace-only rationale is also rejected."""
    with pytest.raises(ValidationError, match="rationale"):
        _make_packet(rationale="   ")


def test_candidate_id_wrong_length_raises() -> None:
    """candidate_id shorter than 64 characters raises ValidationError."""
    with pytest.raises(ValidationError, match="candidate_id"):
        _make_packet(candidate_id="too-short")


def test_whitespace_run_id_raises() -> None:
    """A whitespace-only run_id is rejected."""
    with pytest.raises(ValidationError):
        _make_packet(run_id="   ")


# ---------------------------------------------------------------------------
# is_high_risk / can_transition_to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", [ProposalClass.merge, ProposalClass.delete])
def test_is_high_risk_for_merge_and_delete(cls: ProposalClass) -> None:
    """merge and delete proposals are flagged as high-risk."""
    assert _make_packet(proposal_class=cls).is_high_risk() is True


@pytest.mark.parametrize(
    "cls",
    [ProposalClass.canonicalize, ProposalClass.normalize, ProposalClass.rename, ProposalClass.defer],
)
def test_is_not_high_risk_for_low_risk_classes(cls: ProposalClass) -> None:
    """Low-risk proposal classes are not flagged as high-risk."""
    assert _make_packet(proposal_class=cls).is_high_risk() is False


@pytest.mark.parametrize(
    "target_state",
    [ProposalState.approved, ProposalState.rejected, ProposalState.deferred],
)
def test_pending_can_transition_to_all_non_pending(target_state: ProposalState) -> None:
    """A pending proposal can transition to approved, rejected, or deferred."""
    packet = _make_packet()
    assert packet.can_transition_to(target_state) is True


def test_approved_can_only_transition_to_executed() -> None:
    """An approved proposal can only transition to executed."""
    packet = _make_packet(state=ProposalState.approved)
    assert packet.can_transition_to(ProposalState.executed) is True
    for state in ProposalState:
        if state != ProposalState.executed:
            assert packet.can_transition_to(state) is False


def test_executed_has_no_valid_transitions() -> None:
    """An executed proposal cannot transition to any state (terminal)."""
    packet = _make_packet(state=ProposalState.executed)
    for state in ProposalState:
        assert packet.can_transition_to(state) is False


def test_rejected_has_no_valid_transitions() -> None:
    """A rejected proposal cannot transition to any state (terminal)."""
    packet = _make_packet(state=ProposalState.rejected)
    for state in ProposalState:
        assert packet.can_transition_to(state) is False


def test_deferred_can_reopen_to_pending() -> None:
    """A deferred proposal can be re-opened by transitioning back to pending."""
    packet = _make_packet(state=ProposalState.deferred)
    assert packet.can_transition_to(ProposalState.pending) is True
    assert packet.can_transition_to(ProposalState.approved) is False


def test_allowed_transitions_from_pending() -> None:
    """allowed_transitions(pending) returns the three valid target states."""
    targets = allowed_transitions(ProposalState.pending)
    assert ProposalState.approved in targets
    assert ProposalState.rejected in targets
    assert ProposalState.deferred in targets
    assert ProposalState.pending not in targets


def test_allowed_transitions_from_approved() -> None:
    """allowed_transitions(approved) returns only {executed}."""
    targets = allowed_transitions(ProposalState.approved)
    assert targets == frozenset({ProposalState.executed})
    assert len(targets) == 1


def test_allowed_transitions_from_executed() -> None:
    """allowed_transitions(executed) returns empty set (terminal)."""
    targets = allowed_transitions(ProposalState.executed)
    assert targets == frozenset()
    assert len(targets) == 0


def test_invalid_transition_error_carries_states() -> None:
    """InvalidTransitionError exposes from_state and to_state attributes."""
    err = InvalidTransitionError(ProposalState.approved, ProposalState.pending)
    assert err.from_state == ProposalState.approved
    assert err.to_state == ProposalState.pending


# ---------------------------------------------------------------------------
# ProposalService — service-level tests
# ---------------------------------------------------------------------------


async def test_service_create_returns_packet() -> None:
    """create() persists the packet to S3 and returns it."""
    cache = _FakeCache()
    with patch("api.proposals.service.get_cache_client", return_value=cache):
        svc, s3 = _make_service(cache)
        packet = _make_packet()
        result = await svc.create(packet)

    assert result.proposal_id == packet.proposal_id
    assert result.run_id == _RUN_ID
    # Confirm S3 contains the serialised packet
    s3_key = f"{_RUN_ID}/proposals/{packet.proposal_id}.json"
    assert s3_key in s3._store


async def test_service_create_is_idempotent() -> None:
    """Creating the same proposal twice returns the stored packet unchanged."""
    cache = _FakeCache()
    with patch("api.proposals.service.get_cache_client", return_value=cache):
        svc, _ = _make_service(cache)
        packet = _make_packet()
        result_a = await svc.create(packet)
        result_b = await svc.create(packet)

    assert result_a.proposal_id == result_b.proposal_id
    assert result_b.state == ProposalState.pending


async def test_service_transition_pending_to_approved() -> None:
    """transition() from pending → approved returns a new packet with updated state."""
    cache = _FakeCache()
    with patch("api.proposals.service.get_cache_client", return_value=cache):
        svc, _ = _make_service(cache)
        packet = _make_packet()
        await svc.create(packet)
        updated = await svc.transition(_RUN_ID, packet.proposal_id, ProposalState.approved)

    assert updated.state == ProposalState.approved
    assert updated.proposal_id == packet.proposal_id


async def test_service_transition_pending_to_deferred() -> None:
    """transition() from pending → deferred succeeds."""
    cache = _FakeCache()
    with patch("api.proposals.service.get_cache_client", return_value=cache):
        svc, _ = _make_service(cache)
        packet = _make_packet()
        await svc.create(packet)
        updated = await svc.transition(_RUN_ID, packet.proposal_id, ProposalState.deferred)

    assert updated.state == ProposalState.deferred


async def test_service_transition_deferred_to_pending() -> None:
    """Deferred proposals can be re-opened (deferred → pending)."""
    cache = _FakeCache()
    with patch("api.proposals.service.get_cache_client", return_value=cache):
        svc, _ = _make_service(cache)
        packet = _make_packet()
        await svc.create(packet)
        await svc.transition(_RUN_ID, packet.proposal_id, ProposalState.deferred)
        reopened = await svc.transition(_RUN_ID, packet.proposal_id, ProposalState.pending)

    assert reopened.state == ProposalState.pending


async def test_service_transition_not_found_raises() -> None:
    """transition() on a nonexistent proposal raises ProposalNotFoundError."""
    cache = _FakeCache()
    with patch("api.proposals.service.get_cache_client", return_value=cache):
        svc, _ = _make_service(cache)

    with pytest.raises(ProposalNotFoundError):
        await svc.transition(_RUN_ID, "c" * 64, ProposalState.approved)


async def test_service_transition_invalid_state_raises() -> None:
    """transition() on an approved (terminal) proposal raises InvalidTransitionError."""
    cache = _FakeCache()
    with patch("api.proposals.service.get_cache_client", return_value=cache):
        svc, _ = _make_service(cache)
        packet = _make_packet()
        await svc.create(packet)
        await svc.transition(_RUN_ID, packet.proposal_id, ProposalState.approved)

        with pytest.raises(InvalidTransitionError) as exc_info:
            await svc.transition(_RUN_ID, packet.proposal_id, ProposalState.pending)

    assert exc_info.value.from_state == ProposalState.approved
    assert exc_info.value.to_state == ProposalState.pending


async def test_service_list_for_run_empty_on_missing_index() -> None:
    """list_for_run() returns an empty list when no proposals have been created."""
    cache = _FakeCache()
    with patch("api.proposals.service.get_cache_client", return_value=cache):
        svc, _ = _make_service(cache)
        results = await svc.list_for_run(_RUN_ID)

    assert results == []
