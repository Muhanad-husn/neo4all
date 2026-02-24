"""
api/audit/writer.py — Immutable append-only audit log writer (SPEC-06 S-06.5).

AuditWriter stores AuditRecord instances to S3 exactly once per record_id.
Attempting to write a record whose S3 key already exists raises
AuditRecordConflictError — overwrites are categorically prohibited.

S3 key pattern:
    {run_id}/audit/{record_id}.json

Immutability guarantee:
    Before every write, a HEAD request checks whether the target key exists.
    If it does, the write is rejected.  Combined with AuditRecord.record_id
    being a deterministic hash of (run_id, proposal_id, diff_id, approval_id,
    timestamp_utc), each execution attempt produces a unique key so retries
    do not collide with previous records.

All boto3 calls are wrapped with asyncio.to_thread (boto3 is synchronous).

Public API:
    AuditWriter.append(record)          → s3_key  (write, fail on conflict)
    AuditWriter.list_for_run(run_id)    → list[AuditRecord]  (sorted by timestamp)
"""

from __future__ import annotations

import asyncio
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from api.audit.models import AuditRecord
from api.observability.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public exceptions
# ---------------------------------------------------------------------------


class AuditWriteError(Exception):
    """Raised for unrecoverable S3 failures during audit operations.

    Attributes:
        code:    Machine-readable error code (e.g. "s3_put_failed").
        message: Human-readable description.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AuditRecordConflictError(Exception):
    """Raised when attempting to write a record whose key already exists.

    This is the primary immutability enforcement exception.  Callers must
    never catch and suppress this — it signals a programming error or a
    duplicate execution attempt that the governance layer must handle.

    Attributes:
        record_id: SHA-256 hex digest of the conflicting record.
        s3_key:    The S3 object key that already exists.
    """

    def __init__(self, record_id: str, s3_key: str) -> None:
        super().__init__(
            f"Audit record {record_id!r} already exists at {s3_key!r} — "
            "overwriting is prohibited (immutable append-only log)"
        )
        self.record_id = record_id
        self.s3_key = s3_key


# ---------------------------------------------------------------------------
# S3 key builders — single place for path layout (SKILL-C R-C6)
# ---------------------------------------------------------------------------


def _audit_s3_key(run_id: str, record_id: str) -> str:
    """S3 key for a single serialised AuditRecord."""
    return f"{run_id}/audit/{record_id}.json"


def _audit_prefix(run_id: str) -> str:
    """S3 prefix for all audit records belonging to a run."""
    return f"{run_id}/audit/"


# ---------------------------------------------------------------------------
# Synchronous S3 helpers — always run inside asyncio.to_thread
# ---------------------------------------------------------------------------


def _sync_head_object(client: Any, bucket: str, key: str) -> bool:
    """Return True if the S3 key exists, False if it is absent (404 / NoSuchKey).

    Raises AuditWriteError for all other failures (network, permissions, etc.)
    so the caller knows the existence check itself failed.
    """
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchKey"):
            return False
        raise AuditWriteError(
            code="s3_head_failed",
            message=f"S3 HEAD failed for key {key!r}: {exc}",
        ) from exc
    except BotoCoreError as exc:
        raise AuditWriteError(
            code="s3_head_failed",
            message=f"S3 HEAD failed for key {key!r}: {exc}",
        ) from exc


def _sync_put_audit(client: Any, bucket: str, key: str, body: bytes) -> None:
    """PUT audit JSON to S3; raise AuditWriteError on any failure."""
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
    except (BotoCoreError, ClientError) as exc:
        raise AuditWriteError(
            code="s3_put_failed",
            message=f"S3 PUT failed for audit key {key!r}: {exc}",
        ) from exc


def _sync_get_audit(client: Any, bucket: str, key: str) -> bytes | None:
    """GET audit JSON from S3.

    Returns raw bytes on hit, None on 404 / NoSuchKey.
    Raises AuditWriteError for all other failures.
    """
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404"):
            return None
        raise AuditWriteError(
            code="s3_get_failed",
            message=f"S3 GET failed for audit key {key!r}: {exc}",
        ) from exc
    except BotoCoreError as exc:
        raise AuditWriteError(
            code="s3_get_failed",
            message=f"S3 GET failed for audit key {key!r}: {exc}",
        ) from exc


def _sync_list_audit_keys(client: Any, bucket: str, prefix: str) -> list[str]:
    """List all S3 object keys under the audit prefix for a run.

    Uses the list_objects_v2 paginator to handle runs with many audit
    records without hitting the 1000-object S3 list limit.

    Returns an empty list if the prefix has no objects.
    Raises AuditWriteError on S3 or network failures.
    """
    keys: list[str] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
    except (BotoCoreError, ClientError) as exc:
        raise AuditWriteError(
            code="s3_list_failed",
            message=f"S3 LIST failed for prefix {prefix!r}: {exc}",
        ) from exc
    return keys


# ---------------------------------------------------------------------------
# AuditWriter
# ---------------------------------------------------------------------------


class AuditWriter:
    """Immutable append-only audit log writer.

    Each AuditRecord is written to S3 exactly once.  The writer checks for
    key existence before every PUT — if the key already exists,
    AuditRecordConflictError is raised and no write occurs.

    Since record_id includes timestamp_utc, retrying a failed execution
    naturally produces a new record_id (new timestamp), so idempotency
    does not need to be artificially enforced by the caller.

    S3 key pattern: {run_id}/audit/{record_id}.json

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
    # append
    # ------------------------------------------------------------------

    async def append(self, record: AuditRecord) -> str:
        """Write an AuditRecord to S3 (append-only, never overwrites).

        Steps:
          1. Derive the S3 key from record.run_id and record.record_id.
          2. HEAD the key — raise AuditRecordConflictError if it exists.
          3. PUT the serialised record as application/json.
          4. Emit a structured INFO log event.

        Args:
            record: Fully constructed, frozen AuditRecord instance.

        Returns:
            S3 object key of the written record (for audit trail linkage).

        Raises:
            AuditRecordConflictError: A record with this record_id already exists.
            AuditWriteError:          On S3 HEAD or PUT failure.

        Log events:
            audit_record_written   INFO    — record_id, run_id, proposal_id,
                                             diff_id, outcome, s3_key
            audit_record_conflict  WARNING — record_id, run_id, s3_key
            audit_write_error      ERROR   — record_id, run_id, operation, error
        """
        s3_key = _audit_s3_key(record.run_id, record.record_id)

        # Guard: refuse to overwrite any existing record.
        try:
            exists = await asyncio.to_thread(
                _sync_head_object, self._client, self._bucket, s3_key
            )
        except AuditWriteError as exc:
            logger.error(
                "audit_write_error",
                record_id=record.record_id,
                run_id=record.run_id,
                operation="head",
                error=exc.message,
            )
            raise

        if exists:
            logger.warning(
                "audit_record_conflict",
                record_id=record.record_id,
                run_id=record.run_id,
                s3_key=s3_key,
            )
            raise AuditRecordConflictError(record.record_id, s3_key)

        # Append-only PUT.
        body = record.model_dump_json().encode("utf-8")
        try:
            await asyncio.to_thread(
                _sync_put_audit, self._client, self._bucket, s3_key, body
            )
        except AuditWriteError as exc:
            logger.error(
                "audit_write_error",
                record_id=record.record_id,
                run_id=record.run_id,
                operation="put",
                error=exc.message,
            )
            raise

        logger.info(
            "audit_record_written",
            record_id=record.record_id,
            run_id=record.run_id,
            proposal_id=record.proposal_id,
            diff_id=record.diff_id,
            outcome=str(record.outcome),
            s3_key=s3_key,
        )
        return s3_key

    # ------------------------------------------------------------------
    # list_for_run
    # ------------------------------------------------------------------

    async def list_for_run(self, run_id: str) -> list[AuditRecord]:
        """Return all audit records for a run, sorted by timestamp ascending.

        Performs an S3 prefix listing under {run_id}/audit/ and fetches each
        record.  Records are sorted by timestamp_utc so callers receive a
        chronological audit trail.

        This method is read-only and does not modify any cache or storage state.

        Args:
            run_id: Governed run identifier.

        Returns:
            List of AuditRecord instances sorted by timestamp_utc ascending.
            Empty list if no records exist for this run.

        Raises:
            AuditWriteError: On S3 listing or individual record fetch failure.

        Log events:
            audit_records_listed  INFO  — run_id, count
            audit_list_error      ERROR — run_id, error
        """
        prefix = _audit_prefix(run_id)
        try:
            keys = await asyncio.to_thread(
                _sync_list_audit_keys, self._client, self._bucket, prefix
            )
        except AuditWriteError as exc:
            logger.error(
                "audit_list_error",
                run_id=run_id,
                error=exc.message,
            )
            raise

        records: list[AuditRecord] = []
        for key in keys:
            raw = await asyncio.to_thread(
                _sync_get_audit, self._client, self._bucket, key
            )
            if raw is not None:
                records.append(AuditRecord.model_validate_json(raw))

        records.sort(key=lambda r: r.timestamp_utc)

        logger.info("audit_records_listed", run_id=run_id, count=len(records))
        return records
