"""
tests/unit/test_llm_validation.py — LLMClient fail-closed response handling
(SPEC-02 file 9).

Acceptance criteria (SPEC-02, CLAUDE.md §4.4):
  - _validate_response() returns a model instance on valid JSON with correct structure.
  - _validate_response() returns None on non-JSON text (fail-closed).
  - _validate_response() returns None on valid JSON with the wrong structure.
  - _validate_response() returns None on valid JSON with field type mismatches.
  - _validate_response() returns None on empty string (fail-closed).
  - _validate_response() logs a structured ERROR event on every fallback path.
  - call() returns None on 4xx client errors (non-retryable, returns None immediately).
  - call() returns None after all retry attempts are exhausted on 5xx/network errors.
  - call() returns None after _MAX_ATTEMPTS retries on httpx.TimeoutException.
  - call() returns the parsed model on a valid HTTP response.
  - call() retries on a transient error and succeeds on a subsequent attempt.

No network, no LLM — httpx and asyncio.sleep are mocked throughout.
"""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import httpx
from pydantic import BaseModel

from api.services.llm import (
    JobConfig,
    LLMClient,
    _MAX_ATTEMPTS,
    _NonRetryableError,
    _RetryableError,
)

# ---------------------------------------------------------------------------
# Minimal Pydantic model used as the response_model in tests.
# ---------------------------------------------------------------------------


class _TestModel(BaseModel):
    """Minimal Pydantic model for test response validation."""

    name: str
    count: int


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_JOB = JobConfig(job_id="test_job", model="test/model")
_RUN_ID = "unit-test-run-llm-0000000001"
_API_KEY = "sk-test-key-not-used-in-unit-tests"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client() -> LLMClient:
    """Return a fresh LLMClient with the test API key."""
    return LLMClient(api_key=_API_KEY)


# ---------------------------------------------------------------------------
# Test group 1: _validate_response — JSON parsing and model validation
# (Required test names from SPEC-02 §Files to Generate, file 9)
# ---------------------------------------------------------------------------


def test_valid_response_parsed() -> None:
    """Well-formed LLM output matching the response model returns a model instance."""
    client = _make_client()
    result = client._validate_response(
        raw_text='{"name": "Alice", "count": 42}',
        response_model=_TestModel,
        job_id="test_job",
        run_id=_RUN_ID,
    )

    assert result is not None
    assert isinstance(result, _TestModel)
    assert result.name == "Alice"
    assert result.count == 42


def test_malformed_json_fallback() -> None:
    """Invalid JSON triggers fail-closed fallback — returns None, not an exception."""
    client = _make_client()
    result = client._validate_response(
        raw_text="this is definitely not JSON",
        response_model=_TestModel,
        job_id="test_job",
        run_id=_RUN_ID,
    )

    assert result is None


def test_missing_fields_fallback() -> None:
    """Valid JSON missing required fields triggers fail-closed fallback.

    The payload is syntactically valid JSON but omits both 'name' and 'count',
    so Pydantic validation must reject it and return None.
    """
    client = _make_client()
    result = client._validate_response(
        raw_text='{"foo": "bar"}',  # 'name' and 'count' are absent
        response_model=_TestModel,
        job_id="test_job",
        run_id=_RUN_ID,
    )

    assert result is None


def test_type_mismatch_fallback() -> None:
    """Valid JSON with wrong field types triggers fail-closed fallback.

    'count' is declared int.  Pydantic v2 cannot coerce the string
    "not-a-number" to int, so validation must fail and return None.
    """
    client = _make_client()
    result = client._validate_response(
        raw_text='{"name": "Alice", "count": "not-a-number"}',
        response_model=_TestModel,
        job_id="test_job",
        run_id=_RUN_ID,
    )

    assert result is None


def test_structured_error_logged() -> None:
    """Fallback logs a structured ERROR event via the centralized logger (SKILL-D R-D3).

    Verifies that _validate_response emits exactly one error-level call with
    the machine-readable event name 'llm_call_json_error' and the required
    keyword fields — no free-text strings, no missing keys.
    """
    client = _make_client()
    with patch("api.services.llm.logger") as mock_logger:
        result = client._validate_response(
            raw_text="not json at all",
            response_model=_TestModel,
            job_id="test_job",
            run_id=_RUN_ID,
        )

    assert result is None
    mock_logger.error.assert_called_once_with(
        "llm_call_json_error",
        job_id="test_job",
        run_id=_RUN_ID,
        error=ANY,
        raw_length=ANY,
    )


# ---------------------------------------------------------------------------
# Test group 1b: structured validation-error logging
# ---------------------------------------------------------------------------


def test_structured_validation_error_logged() -> None:
    """Pydantic validation failure logs 'llm_call_validation_error' at ERROR level.

    Confirms the correct event name and that error_count (a count, not the
    full Pydantic exception object) is included in the structured payload.
    """
    client = _make_client()
    with patch("api.services.llm.logger") as mock_logger:
        result = client._validate_response(
            raw_text='{"foo": "bar"}',  # valid JSON, wrong structure
            response_model=_TestModel,
            job_id="test_job",
            run_id=_RUN_ID,
        )

    assert result is None
    mock_logger.error.assert_called_once_with(
        "llm_call_validation_error",
        job_id="test_job",
        run_id=_RUN_ID,
        error=ANY,
        error_count=ANY,
    )


# ---------------------------------------------------------------------------
# Test group 1c: additional _validate_response edge cases
# ---------------------------------------------------------------------------


def test_validate_response_empty_string_returns_none() -> None:
    """Empty string fails JSON parsing and returns None."""
    client = _make_client()
    result = client._validate_response(
        raw_text="",
        response_model=_TestModel,
        job_id="test_job",
        run_id=_RUN_ID,
    )

    assert result is None


def test_validate_response_null_root_returns_none() -> None:
    """JSON null at root level fails Pydantic model validation → None."""
    client = _make_client()
    result = client._validate_response(
        raw_text="null",
        response_model=_TestModel,
        job_id="test_job",
        run_id=_RUN_ID,
    )

    assert result is None


def test_validate_response_extra_fields_accepted() -> None:
    """Extra JSON fields beyond the model schema are silently ignored.

    Pydantic v2 drops extra fields by default (extra='ignore').  Valid required
    fields must still be parsed and returned correctly.
    """
    client = _make_client()
    result = client._validate_response(
        raw_text='{"name": "Bob", "count": 7, "extra_key": "ignored"}',
        response_model=_TestModel,
        job_id="test_job",
        run_id=_RUN_ID,
    )

    assert result is not None
    assert result.name == "Bob"
    assert result.count == 7


def test_validate_response_markdown_fenced_json_returns_none() -> None:
    """JSON wrapped in markdown code fences is invalid raw_text → None.

    The LLM prompt instructs 'Return ONLY valid JSON'.  If the model wraps
    output in fences anyway, _validate_response must reject it rather than
    silently parse partial content — fail-closed (CLAUDE.md §4.4).
    """
    client = _make_client()
    result = client._validate_response(
        raw_text='```json\n{"name": "Carol", "count": 3}\n```',
        response_model=_TestModel,
        job_id="test_job",
        run_id=_RUN_ID,
    )

    assert result is None


# ---------------------------------------------------------------------------
# Test group 2: call() — HTTP-level behaviour
# (Required test name: test_retry_on_timeout)
# ---------------------------------------------------------------------------


async def test_retry_on_timeout() -> None:
    """httpx.TimeoutException triggers retry up to _MAX_ATTEMPTS, then returns None.

    httpx.TimeoutException is caught inside _send_request and converted to
    _RetryableError.  call() must retry the full _MAX_ATTEMPTS times before
    returning None.

    Strategy: inject a MagicMock as the internal httpx client so that .post()
    raises httpx.TimeoutException on every attempt.  asyncio.sleep is patched
    to keep the test fast.
    """
    client = _make_client()

    mock_http = MagicMock()
    mock_http.is_closed = False
    mock_http.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    client._http = mock_http

    with patch("asyncio.sleep", new_callable=AsyncMock):
        result = await client.call(
            job=_JOB,
            system_prompt="system",
            user_message="user",
            response_model=_TestModel,
            run_id=_RUN_ID,
        )

    assert result is None
    assert mock_http.post.call_count == _MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Test group 2b: additional call() behaviour
# ---------------------------------------------------------------------------


async def test_call_returns_model_on_valid_response() -> None:
    """call() returns a validated model instance on a successful HTTP response."""
    client = _make_client()
    with patch.object(
        client,
        "_send_request",
        new=AsyncMock(return_value='{"name": "Alice", "count": 5}'),
    ):
        result = await client.call(
            job=_JOB,
            system_prompt="system",
            user_message="user",
            response_model=_TestModel,
            run_id=_RUN_ID,
        )

    assert result is not None
    assert result.name == "Alice"
    assert result.count == 5


async def test_call_returns_none_on_invalid_json_in_response() -> None:
    """call() returns None when the HTTP response body is not valid JSON.

    The LLM may produce malformed output despite the prompt instruction.
    call() must fail closed rather than raise an exception (CLAUDE.md §4.4).
    """
    client = _make_client()
    with patch.object(
        client,
        "_send_request",
        new=AsyncMock(return_value="not valid JSON at all!"),
    ):
        result = await client.call(
            job=_JOB,
            system_prompt="system",
            user_message="user",
            response_model=_TestModel,
            run_id=_RUN_ID,
        )

    assert result is None


async def test_call_returns_none_on_schema_mismatch_response() -> None:
    """call() returns None when the JSON response fails model validation."""
    client = _make_client()
    with patch.object(
        client,
        "_send_request",
        new=AsyncMock(return_value='{"unexpected_field": 123}'),
    ):
        result = await client.call(
            job=_JOB,
            system_prompt="system",
            user_message="user",
            response_model=_TestModel,
            run_id=_RUN_ID,
        )

    assert result is None


async def test_call_returns_none_on_non_retryable_4xx_error() -> None:
    """call() returns None immediately on a 4xx client error (non-retryable).

    A 4xx error signals a client-side mistake (bad API key, invalid request)
    that will not be resolved by retrying.
    """
    client = _make_client()
    with patch.object(
        client,
        "_send_request",
        new=AsyncMock(side_effect=_NonRetryableError("client_error", status_code=401)),
    ):
        result = await client.call(
            job=_JOB,
            system_prompt="system",
            user_message="user",
            response_model=_TestModel,
            run_id=_RUN_ID,
        )

    assert result is None


async def test_call_does_not_retry_on_4xx_client_error() -> None:
    """call() sends exactly one request on a 4xx error — no retry attempts.

    Retrying 4xx responses would be wasteful and could cause API-key lockout.
    """
    client = _make_client()
    send_mock = AsyncMock(
        side_effect=_NonRetryableError("client_error status=401", status_code=401)
    )

    with patch.object(client, "_send_request", new=send_mock):
        await client.call(
            job=_JOB,
            system_prompt="system",
            user_message="user",
            response_model=_TestModel,
            run_id=_RUN_ID,
        )

    assert send_mock.call_count == 1


async def test_call_returns_none_after_all_retries_exhausted() -> None:
    """call() returns None after _MAX_ATTEMPTS retryable failures.

    asyncio.sleep is patched to prevent actual delays during the test.
    """
    client = _make_client()
    with patch.object(
        client,
        "_send_request",
        new=AsyncMock(side_effect=_RetryableError("server_error status=503")),
    ), patch("asyncio.sleep", new_callable=AsyncMock):
        result = await client.call(
            job=_JOB,
            system_prompt="system",
            user_message="user",
            response_model=_TestModel,
            run_id=_RUN_ID,
        )

    assert result is None


async def test_call_retries_correct_number_of_times() -> None:
    """call() sends exactly _MAX_ATTEMPTS requests before giving up on 5xx errors."""
    client = _make_client()
    send_mock = AsyncMock(side_effect=_RetryableError("server_error status=503"))

    with patch.object(client, "_send_request", new=send_mock), patch(
        "asyncio.sleep", new_callable=AsyncMock
    ):
        await client.call(
            job=_JOB,
            system_prompt="system",
            user_message="user",
            response_model=_TestModel,
            run_id=_RUN_ID,
        )

    assert send_mock.call_count == _MAX_ATTEMPTS


async def test_call_retries_on_transient_error_then_succeeds() -> None:
    """call() retries after a transient error and returns the model on success.

    Simulates one 5xx failure followed by a valid response on the second attempt.
    """
    client = _make_client()
    valid_response = '{"name": "Bob", "count": 3}'
    send_mock = AsyncMock(
        side_effect=[
            _RetryableError("server_error status=500"),
            valid_response,
        ]
    )

    with patch.object(client, "_send_request", new=send_mock), patch(
        "asyncio.sleep", new_callable=AsyncMock
    ):
        result = await client.call(
            job=_JOB,
            system_prompt="system",
            user_message="user",
            response_model=_TestModel,
            run_id=_RUN_ID,
        )

    assert result is not None
    assert result.name == "Bob"
    assert result.count == 3
    assert send_mock.call_count == 2  # one failure, one success
