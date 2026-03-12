"""
api/services/llm.py — Typed OpenRouter LLM client (SPEC-02 S-02.3).

Responsibilities
----------------
  - Sends chat-completion requests to OpenRouter via httpx.AsyncClient.
  - Validates the raw JSON response against a caller-supplied Pydantic model.
  - Fails closed on malformed output: logs a structured error and returns None
    so callers can emit their own fallback (CLAUDE.md §4.4).
  - Retries transient HTTP/network errors with exponential backoff (3 attempts).
  - Logs every LLM call as a structured event (SKILL-D R-D3).

Fail-closed contract
--------------------
  call() returns None when:
    - The HTTP request fails after all retries.
    - The response status code is not 2xx.
    - The response body cannot be JSON-decoded.
    - The decoded JSON fails Pydantic validation against `response_model`.

  The caller is responsible for the fallback action (log, raise, return
  partial result) — this module never silently coerces or auto-corrects.

Sensitive data
--------------
  The OPENROUTER_API_KEY is never logged (SKILL-D R-D5).
  LLM prompts and responses are logged at DEBUG level only, which is disabled
  in production (LOG_LEVEL=INFO or above).

Model configuration
-------------------
  Each job type specifies its model in a JobConfig object.  This avoids
  hardcoded model strings scattered across agent code.

Usage:
    from api.services.llm import LLMClient, JobConfig
    from api.schema.models import SchemaVersion

    client = LLMClient()
    result: MyResponse | None = await client.call(
        job=JobConfig(job_id="schema_propose", model="openai/gpt-4o-mini"),
        system_prompt="...",
        user_message="...",
        response_model=MyResponse,
        run_id=run_id,
    )
    if result is None:
        # fail-closed: caller handles fallback
        raise ValueError("LLM returned invalid output")
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from api.observability.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPENROUTER_URL: str = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_TIMEOUT_S: float = 60.0
_MAX_ATTEMPTS: int = 3
_BACKOFF_BASE_S: float = 1.0  # seconds; attempt N waits BASE * 2^(N-1)


# ---------------------------------------------------------------------------
# Job configuration
# ---------------------------------------------------------------------------

class JobConfig(BaseModel):
    """Per-job LLM model and parameter configuration.

    Decouples model selection from agent code so that prompt templates and
    model choices can be updated without touching business logic.

    Attributes:
        job_id:        Identifies the job type; used for logging and prompt
                       template resolution (e.g. "schema_propose").
        model:         OpenRouter model identifier
                       (e.g. "openai/gpt-4o-mini", "anthropic/claude-3-haiku").
        temperature:   Sampling temperature.  Default 0.2 for low-variance
                       structured outputs.
        max_tokens:    Hard cap on response tokens.  None lets the model decide.
        response_format: Optional dict passed verbatim to OpenRouter
                         (e.g. {"type": "json_object"} for JSON mode).
    """

    job_id: str
    model: str
    temperature: float = 0.2
    max_tokens: int | None = None
    response_format: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# LLM client
# ---------------------------------------------------------------------------

class LLMClient:
    """Async OpenRouter client with typed response validation.

    Each call() invocation creates fresh httpx request objects but shares the
    underlying HTTPX connection pool across calls via a lazily-created
    AsyncClient that lives for the lifetime of the LLMClient instance.

    Lifecycle:
        The client is intended to be used as a dependency-injected singleton.
        Call aclose() in the application shutdown hook to drain the connection
        pool cleanly.

    Args:
        api_key:    OpenRouter API key.  Defaults to OPENROUTER_API_KEY env var
                    via api.config.get_settings() (resolved lazily on first
                    call, not at construction time).
        timeout_s:  Per-request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key: str | None = api_key
        self._timeout_s: float = timeout_s
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_api_key(self) -> str:
        from api.credentials import get_credentials
        key = self._api_key or get_credentials().openrouter_api_key
        if not key.strip():
            raise RuntimeError("OPENROUTER_API_KEY is not configured")
        return key

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self._timeout_s)
        return self._http

    def _build_payload(
        self,
        job: JobConfig,
        system_prompt: str,
        user_message: str,
    ) -> dict[str, Any]:
        """Assemble the OpenRouter chat-completions request payload."""
        payload: dict[str, Any] = {
            "model": job.model,
            "temperature": job.temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        if job.max_tokens is not None:
            payload["max_tokens"] = job.max_tokens
        if job.response_format is not None:
            payload["response_format"] = job.response_format
        return payload

    # ------------------------------------------------------------------
    # Core call with retry
    # ------------------------------------------------------------------

    async def call(
        self,
        job: JobConfig,
        system_prompt: str,
        user_message: str,
        response_model: type[T],
        run_id: str = "",
    ) -> T | None:
        """Send a chat-completion request and validate the structured response.

        Retries up to _MAX_ATTEMPTS times on transient errors (connection
        failures, timeouts, 5xx responses) with exponential backoff.

        Non-retryable failures (4xx client errors, validation failures) are
        logged and return None immediately.

        Args:
            job:            Job configuration (model, temperature, etc.).
            system_prompt:  System-role message content.
            user_message:   User-role message content.
            response_model: Pydantic model class to validate the parsed JSON
                            response against.
            run_id:         Governs the correlation ID emitted in log entries.
                            Pass "" when no run context is available.

        Returns:
            A validated instance of response_model, or None on any failure.

        Log events (SKILL-D R-D3):
            llm_call_start          DEBUG — job_id, model, run_id
            llm_call_attempt        DEBUG — job_id, attempt, model
            llm_call_retry          WARNING — job_id, attempt, error
            llm_call_client_error   ERROR   — job_id, status_code, run_id
            llm_call_json_error     ERROR   — job_id, run_id, error
            llm_call_validation_error ERROR — job_id, run_id, error
            llm_call_complete       DEBUG   — job_id, model, run_id, duration_ms
            llm_call_failed         ERROR   — job_id, run_id, attempts
        """
        logger.debug(
            "llm_call_start",
            job_id=job.job_id,
            model=job.model,
            run_id=run_id,
        )

        payload = self._build_payload(job, system_prompt, user_message)

        # DEBUG-only: log prompt content — disabled at INFO+ (SKILL-D R-D5)
        logger.debug(
            "llm_call_prompt",
            job_id=job.job_id,
            run_id=run_id,
            system_length=len(system_prompt),
            user_length=len(user_message),
        )

        t0 = time.perf_counter()

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            logger.debug(
                "llm_call_attempt",
                job_id=job.job_id,
                attempt=attempt,
                model=job.model,
            )

            try:
                raw_text = await self._send_request(payload)
            except _RetryableError as exc:
                if attempt < _MAX_ATTEMPTS:
                    wait_s = _BACKOFF_BASE_S * (2 ** (attempt - 1))
                    logger.warning(
                        "llm_call_retry",
                        job_id=job.job_id,
                        attempt=attempt,
                        wait_s=wait_s,
                        error=str(exc),
                        run_id=run_id,
                    )
                    await asyncio.sleep(wait_s)
                    continue
                # All attempts exhausted.
                logger.error(
                    "llm_call_failed",
                    job_id=job.job_id,
                    run_id=run_id,
                    attempts=attempt,
                    error=str(exc),
                )
                return None
            except _NonRetryableError as exc:
                logger.error(
                    "llm_call_client_error",
                    job_id=job.job_id,
                    run_id=run_id,
                    status_code=exc.status_code,
                    error=str(exc),
                )
                return None

            # HTTP succeeded — validate the response.
            validated = self._validate_response(
                raw_text=raw_text,
                response_model=response_model,
                job_id=job.job_id,
                run_id=run_id,
            )
            if validated is None:
                # Short garbage responses (e.g. "None", empty) are likely
                # transient LLM glitches — retry them.  Longer responses
                # that fail validation have a structural mismatch and
                # retrying is unlikely to help.
                if len(raw_text) < 16 and attempt < _MAX_ATTEMPTS:
                    wait_s = _BACKOFF_BASE_S * (2 ** (attempt - 1))
                    logger.warning(
                        "llm_call_retry_short_response",
                        job_id=job.job_id,
                        attempt=attempt,
                        raw_length=len(raw_text),
                        wait_s=wait_s,
                        run_id=run_id,
                    )
                    await asyncio.sleep(wait_s)
                    continue
                return None

            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            logger.debug(
                "llm_call_complete",
                job_id=job.job_id,
                model=job.model,
                run_id=run_id,
                duration_ms=duration_ms,
                attempt=attempt,
            )
            return validated

        # Should be unreachable; kept for type-checker satisfaction.
        return None  # pragma: no cover

    # ------------------------------------------------------------------
    # HTTP dispatch
    # ------------------------------------------------------------------

    async def _send_request(self, payload: dict[str, Any]) -> str:
        """POST the payload to OpenRouter and return the assistant text.

        Raises:
            _RetryableError: On network errors or 5xx responses.
            _NonRetryableError: On 4xx client errors.
        """
        http = self._get_http_client()
        headers = {
            "Authorization": f"Bearer {self._resolve_api_key()}",
            "Content-Type": "application/json",
        }
        # API key is in headers and must never appear in logs (SKILL-D R-D5).
        try:
            response = await http.post(
                _OPENROUTER_URL,
                json=payload,
                headers=headers,
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise _RetryableError(str(exc)) from exc

        if response.status_code >= 500:
            raise _RetryableError(
                f"server_error status={response.status_code}"
            )
        if response.status_code >= 400:
            raise _NonRetryableError(
                f"client_error status={response.status_code}",
                status_code=response.status_code,
            )

        # Extract assistant message content from the choices array.
        try:
            body: dict[str, Any] = response.json()
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, ValueError) as exc:
            # Treat unexpected response shapes as retryable — transient proxy
            # issues can produce truncated JSON.
            raise _RetryableError(f"malformed_response_body: {exc}") from exc

    # ------------------------------------------------------------------
    # Response validation (fail-closed)
    # ------------------------------------------------------------------

    def _validate_response(
        self,
        raw_text: str,
        response_model: type[T],
        job_id: str,
        run_id: str,
    ) -> T | None:
        """Parse raw_text as JSON and validate against response_model.

        Returns None (fail-closed) on any parsing or validation error.
        Never raises to the caller.

        Log events:
            llm_response_raw        DEBUG — raw content length only (not text)
            llm_call_json_error     ERROR — job_id, run_id, error
            llm_call_validation_error ERROR — job_id, run_id, error
        """
        logger.debug(
            "llm_response_raw",
            job_id=job_id,
            run_id=run_id,
            raw_length=len(raw_text),
        )

        # Strip markdown code fences that some models wrap around JSON.
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            # Remove opening fence (```json or ```)
            first_nl = cleaned.find("\n")
            if first_nl != -1:
                cleaned = cleaned[first_nl + 1:]
            # Remove closing fence
            if cleaned.rstrip().endswith("```"):
                cleaned = cleaned.rstrip()[:-3].rstrip()

        # Step 1: Parse as JSON.
        try:
            parsed: Any = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error(
                "llm_call_json_error",
                job_id=job_id,
                run_id=run_id,
                error=str(exc),
                raw_length=len(raw_text),
            )
            return None

        # Step 2: Validate against the caller-supplied Pydantic model.
        # TypeAdapter handles both BaseModel subclasses and generic types.
        try:
            adapter: TypeAdapter[T] = TypeAdapter(response_model)
            return adapter.validate_python(parsed)
        except ValidationError as exc:
            logger.error(
                "llm_call_validation_error",
                job_id=job_id,
                run_id=run_id,
                error=str(exc),
                error_count=exc.error_count(),
            )
            return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Drain the underlying HTTPX connection pool.

        Call from the FastAPI lifespan shutdown hook.
        """
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
            self._http = None


# ---------------------------------------------------------------------------
# Internal sentinel exceptions — not part of the public API
# ---------------------------------------------------------------------------

class _RetryableError(Exception):
    """Signals a transient error that may succeed on a subsequent attempt."""


class _NonRetryableError(Exception):
    """Signals a client-side error that will not improve with retries."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
