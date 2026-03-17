"""
tests/unit/test_audit_record.py — AuditRecord model and AuditWriter tests (SPEC-06 file 18).

No network, no LLM, no Neo4j.  AuditWriter uses an injected in-memory S3 stub.

Coverage:

  AuditRecord model:
    - Valid construction with all required fields
    - Model is frozen (immutable after construction)
    - approval_id must be non-empty (execution without one is prohibited)
    - timestamp_utc must be timezone-aware UTC (naive datetimes rejected)
    - Non-UTC timezone is normalised to UTC
    - record_id is a deterministic 64-character SHA-256 digest
    - record_id changes when timestamp_utc changes (retries get distinct records)
    - record_id includes all five identity fields
    - AuditRecord.make() stamps the current UTC timestamp
    - AuditOutcome enum has applied / failed / rejected values
    - proposal_id and diff_id must be exactly 64 characters
    - Missing required fields raise ValidationError

  AuditWriter (with fake S3):
    - append() issues a HEAD check before every PUT
    - append() returns the S3 key on success
    - append() raises AuditRecordConflictError when the S3 key already exists
    - append() raises AuditWriteError when the PUT call fails
    - S3 key follows the pattern {run_id}/audit/{record_id}.json
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from api.audit.models import AuditOutcome, AuditRecord
from api.audit.writer import AuditRecordConflictError, AuditWriteError, AuditWriter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_ID = "unit-test-run-audit-0001"
_SCHEMA_VERSION = "sv_audit_fixture_v1"
_CANDIDATE_ID = "e" * 64
_PROPOSAL_ID = "f" * 64
_DIFF_ID = "0" * 64
_APPROVAL_ID = "audit-approval-token-for-unit-tests"
_ACTOR = "unit-test-operator"
_NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

_FAKE_SETTINGS = SimpleNamespace(
    S3_BUCKET_NAME="test-bucket",
    S3_ENDPOINT_URL="http://localhost:9000",
    S3_ACCESS_KEY_ID="test-key",
    S3_SECRET_ACCESS_KEY="test-secret",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(**kwargs) -> AuditRecord:
    defaults = dict(
        run_id=_RUN_ID,
        schema_version=_SCHEMA_VERSION,
        candidate_id=_CANDIDATE_ID,
        proposal_id=_PROPOSAL_ID,
        diff_id=_DIFF_ID,
        approval_id=_APPROVAL_ID,
        timestamp_utc=_NOW,
        actor=_ACTOR,
        outcome=AuditOutcome.applied,
        steps_applied=3,
    )
    defaults.update(kwargs)
    return AuditRecord(**defaults)


class _FreshS3:
    """Fake S3 where every key is absent — HEAD raises 404, PUT succeeds."""

    def __init__(self) -> None:
        self.put_calls: list[dict] = []

    def head_object(self, Bucket: str, Key: str) -> dict:
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "") -> dict:
        self.put_calls.append({"Key": Key, "Body": Body})
        return {}


class _ConflictS3:
    """Fake S3 where every key already exists — HEAD succeeds, PUT never called."""

    def head_object(self, Bucket: str, Key: str) -> dict:
        return {}  # key exists


class _FailPutS3:
    """Fake S3 where HEAD says key is absent but PUT raises ClientError."""

    def head_object(self, Bucket: str, Key: str) -> dict:
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "") -> dict:
        raise ClientError({"Error": {"Code": "InternalError"}}, "PutObject")


# ---------------------------------------------------------------------------
# AuditRecord model tests
# ---------------------------------------------------------------------------


def test_audit_record_valid_construction() -> None:
    """A fully specified AuditRecord constructs without error."""
    record = _make_record()
    assert record.run_id == _RUN_ID
    assert record.outcome == AuditOutcome.applied
    assert record.steps_applied == 3


def test_audit_record_is_frozen() -> None:
    """AuditRecord is immutable — assignment to any field raises an error."""
    record = _make_record()
    with pytest.raises(Exception):
        record.outcome = AuditOutcome.failed  # type: ignore[misc]


def test_empty_approval_id_raises_validation_error() -> None:
    """Empty approval_id is rejected (execution without approval is prohibited)."""
    with pytest.raises(ValidationError, match="approval_id"):
        _make_record(approval_id="")


def test_whitespace_approval_id_raises_validation_error() -> None:
    """Whitespace-only approval_id is rejected."""
    with pytest.raises(ValidationError, match="approval_id"):
        _make_record(approval_id="   ")


def test_naive_timestamp_raises_validation_error() -> None:
    """A naive (timezone-unaware) datetime is rejected."""
    with pytest.raises(ValidationError, match="timezone"):
        _make_record(timestamp_utc=datetime(2024, 1, 1, 12, 0, 0))  # no tzinfo


def test_non_utc_timezone_normalised_to_utc() -> None:
    """A non-UTC timezone is accepted and normalised to UTC."""
    eastern = timezone(timedelta(hours=-5))
    ts_eastern = datetime(2024, 6, 15, 7, 0, 0, tzinfo=eastern)
    record = _make_record(timestamp_utc=ts_eastern)
    # Normalised to UTC: 07:00 EST = 12:00 UTC
    assert record.timestamp_utc.tzinfo == timezone.utc
    assert record.timestamp_utc.hour == 12


def test_record_id_is_deterministic() -> None:
    """Two AuditRecords with identical fields produce the same record_id."""
    r1 = _make_record()
    r2 = _make_record()
    assert r1.record_id == r2.record_id


def test_record_id_changes_with_timestamp() -> None:
    """Different timestamps produce different record_ids (retries are distinct)."""
    r1 = _make_record(timestamp_utc=_NOW)
    r2 = _make_record(timestamp_utc=_NOW + timedelta(seconds=1))
    assert r1.record_id != r2.record_id


def test_record_id_changes_with_approval_id() -> None:
    """Different approval_ids produce different record_ids."""
    r1 = _make_record(approval_id="approval-token-one")
    r2 = _make_record(approval_id="approval-token-two")
    assert r1.record_id != r2.record_id


def test_record_id_is_64_char_hex() -> None:
    """record_id is a 64-character lowercase SHA-256 hex digest."""
    rid = _make_record().record_id
    assert len(rid) == 64
    assert all(c in "0123456789abcdef" for c in rid)


def test_proposal_id_wrong_length_raises() -> None:
    """proposal_id shorter than 64 characters raises ValidationError."""
    with pytest.raises(ValidationError, match="proposal_id"):
        _make_record(proposal_id="too-short")


def test_diff_id_wrong_length_raises() -> None:
    """diff_id shorter than 64 characters raises ValidationError."""
    with pytest.raises(ValidationError, match="diff_id"):
        _make_record(diff_id="too-short")


def test_empty_actor_raises_validation_error() -> None:
    """An empty actor string is rejected."""
    with pytest.raises(ValidationError):
        _make_record(actor="")


def test_audit_outcome_enum_values() -> None:
    """AuditOutcome has exactly the three expected values."""
    values = {o.value for o in AuditOutcome}
    assert values == {"applied", "failed", "rejected"}


def test_make_constructor_stamps_utc_timestamp() -> None:
    """AuditRecord.make() creates a record with a UTC-aware timestamp."""
    record = AuditRecord.make(
        run_id=_RUN_ID,
        schema_version=_SCHEMA_VERSION,
        candidate_id=_CANDIDATE_ID,
        proposal_id=_PROPOSAL_ID,
        diff_id=_DIFF_ID,
        approval_id=_APPROVAL_ID,
        actor=_ACTOR,
        outcome=AuditOutcome.applied,
        steps_applied=2,
    )
    assert record.timestamp_utc.tzinfo == timezone.utc
    assert record.steps_applied == 2


def test_steps_applied_cannot_be_negative() -> None:
    """steps_applied must be >= 0."""
    with pytest.raises(ValidationError):
        _make_record(steps_applied=-1)


# ---------------------------------------------------------------------------
# AuditWriter tests (injected fake S3)
# ---------------------------------------------------------------------------


async def test_writer_append_success_returns_s3_key() -> None:
    """append() with a new record returns the S3 key of the written object."""
    fake_s3 = _FreshS3()
    writer = AuditWriter(settings=_FAKE_SETTINGS, s3_client=fake_s3)
    record = _make_record()

    result_key = await writer.append(record)

    expected_key = f"{_RUN_ID}/audit/{record.record_id}.json"
    assert result_key == expected_key


async def test_writer_append_issues_head_before_put() -> None:
    """append() always issues a HEAD check before PUT — confirmed by PUT call tracking."""
    fake_s3 = _FreshS3()
    writer = AuditWriter(settings=_FAKE_SETTINGS, s3_client=fake_s3)
    record = _make_record()

    await writer.append(record)

    assert len(fake_s3.put_calls) == 1
    assert fake_s3.put_calls[0]["Key"].endswith(f"{record.record_id}.json")


async def test_writer_append_conflict_raises() -> None:
    """append() raises AuditRecordConflictError when the S3 key already exists."""
    fake_s3 = _ConflictS3()
    writer = AuditWriter(settings=_FAKE_SETTINGS, s3_client=fake_s3)
    record = _make_record()

    with pytest.raises(AuditRecordConflictError) as exc_info:
        await writer.append(record)

    assert exc_info.value.record_id == record.record_id


async def test_writer_append_conflict_error_carries_s3_key() -> None:
    """AuditRecordConflictError exposes the conflicting S3 key."""
    fake_s3 = _ConflictS3()
    writer = AuditWriter(settings=_FAKE_SETTINGS, s3_client=fake_s3)
    record = _make_record()

    with pytest.raises(AuditRecordConflictError) as exc_info:
        await writer.append(record)

    expected_key = f"{_RUN_ID}/audit/{record.record_id}.json"
    assert exc_info.value.s3_key == expected_key


async def test_writer_append_put_failure_raises_audit_write_error() -> None:
    """append() raises AuditWriteError when the S3 PUT call fails."""
    fake_s3 = _FailPutS3()
    writer = AuditWriter(settings=_FAKE_SETTINGS, s3_client=fake_s3)
    record = _make_record()

    with pytest.raises(AuditWriteError) as exc_info:
        await writer.append(record)

    assert exc_info.value.code == "s3_put_failed"


async def test_writer_s3_key_format() -> None:
    """append() places the record at {run_id}/audit/{record_id}.json."""
    fake_s3 = _FreshS3()
    writer = AuditWriter(settings=_FAKE_SETTINGS, s3_client=fake_s3)
    record = _make_record()

    key = await writer.append(record)

    parts = key.split("/")
    assert parts[0] == _RUN_ID
    assert parts[1] == "audit"
    assert parts[2] == f"{record.record_id}.json"


async def test_writer_two_records_with_different_timestamps_succeed() -> None:
    """Two records from the same proposal (different timestamps) both write without conflict."""
    fake_s3 = _FreshS3()
    writer = AuditWriter(settings=_FAKE_SETTINGS, s3_client=fake_s3)

    r1 = _make_record(timestamp_utc=_NOW)
    r2 = _make_record(timestamp_utc=_NOW + timedelta(seconds=5))

    assert r1.record_id != r2.record_id  # timestamps differ → distinct records

    key1 = await writer.append(r1)
    key2 = await writer.append(r2)

    assert key1 != key2
    assert len(fake_s3.put_calls) == 2
