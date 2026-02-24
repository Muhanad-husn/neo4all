"""
api/graph/client.py — Async Neo4j driver wrapper (SPEC-04 S-04.4).

Wraps neo4j.AsyncGraphDatabase.driver with:
  - Pool configuration from Settings (NEO4J_MAX_POOL_SIZE,
    NEO4J_CONNECTION_TIMEOUT_S).
  - acquire_session() async context manager for callers needing a direct
    session (e.g. read queries).
  - run_write_transaction() with retry on ServiceUnavailable / SessionExpired
    (up to _MAX_RETRIES attempts, exponential backoff). The neo4j driver
    itself handles TransientError retries inside execute_write — this wrapper
    only handles connection-level failures the driver does not retry.
  - verify_connectivity() for startup health probes (never raises).

Lifecycle
---------
Call open() once at application startup (FastAPI lifespan hook). Call close()
in the shutdown hook. The singleton returned by get_neo4j_client() is intended
for the full process lifetime.

Retry policy
------------
  - _MAX_RETRIES = 3  (1 initial attempt + 2 retries)
  - Backoff: attempt N waits _BACKOFF_BASE_S * 2^(N-1) seconds before retry
  - Catches: ServiceUnavailable, SessionExpired
  - Propagates immediately: all other neo4j exceptions

Sensitive data (SKILL-D R-D5)
------------------------------
The URI is logged only as its host/path fragment. User credentials are never
logged. Connection settings (pool size, timeout) are safe to log.

New config fields (CLAUDE.md §9 env vars)
------------------------------------------
  NEO4J_MAX_POOL_SIZE           (default 50)
  NEO4J_CONNECTION_TIMEOUT_S    (default 30.0 seconds)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, AsyncIterator, Callable, Coroutine, TypeVar

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession
from neo4j.exceptions import ServiceUnavailable, SessionExpired

from api.observability.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

_MAX_RETRIES: int = 3
_BACKOFF_BASE_S: float = 1.0  # seconds; attempt N waits BASE * 2^(N-1)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Neo4jClient:
    """Async Neo4j driver wrapper with connection pooling and retry.

    Instantiate once (via get_neo4j_client()) and share across the application.
    open() must be called before any session is acquired; call close() during
    application shutdown to drain the connection pool.

    Constructor arguments are all optional. Any omitted value is resolved from
    Settings at the time open() is called (lazy — not at construction time so
    unit tests can construct the client without environment variables present).

    Args:
        uri:                  Neo4j connection URI (e.g. "neo4j+s://...").
                              Defaults to NEO4J_DEV_URI.
        user:                 Neo4j username. Defaults to NEO4J_DEV_USER.
        password:             Neo4j password. Defaults to NEO4J_DEV_PASSWORD.
        max_pool_size:        Max connections in the driver pool.
                              Defaults to NEO4J_MAX_POOL_SIZE (50).
        connection_timeout_s: Seconds before a connection attempt is abandoned.
                              Defaults to NEO4J_CONNECTION_TIMEOUT_S (30.0).
    """

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
        max_pool_size: int | None = None,
        connection_timeout_s: float | None = None,
    ) -> None:
        self._uri = uri
        self._user = user
        self._password = password
        self._max_pool_size = max_pool_size
        self._connection_timeout_s = connection_timeout_s
        self._driver: AsyncDriver | None = None

    # ------------------------------------------------------------------
    # Settings resolution — deferred to avoid requiring env vars at import
    # ------------------------------------------------------------------

    def _resolve_settings(self) -> tuple[str, str, str, int, float]:
        from api.config import get_settings

        s = get_settings()
        return (
            self._uri or s.NEO4J_DEV_URI,
            self._user or s.NEO4J_DEV_USER,
            self._password or s.NEO4J_DEV_PASSWORD,
            self._max_pool_size if self._max_pool_size is not None else s.NEO4J_MAX_POOL_SIZE,
            (
                self._connection_timeout_s
                if self._connection_timeout_s is not None
                else s.NEO4J_CONNECTION_TIMEOUT_S
            ),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Create the async driver. Call once at FastAPI lifespan startup.

        Idempotent — calling open() on an already-open client is a no-op.

        The URI may contain credentials in the authority component; only the
        host/path portion is written to the log (SKILL-D R-D5).

        Log events:
            neo4j_driver_opened  INFO  — uri_host, max_pool_size,
                                         connection_timeout_s
        """
        if self._driver is not None:
            return

        uri, user, password, max_pool_size, connection_timeout_s = self._resolve_settings()
        self._driver = AsyncGraphDatabase.driver(
            uri,
            auth=(user, password),
            max_connection_pool_size=max_pool_size,
            connection_timeout=connection_timeout_s,
        )

        # Log only the host fragment — never the full URI or credentials.
        safe_uri = uri.split("@")[-1] if "@" in uri else uri[:40]
        logger.info(
            "neo4j_driver_opened",
            uri_host=safe_uri,
            max_pool_size=max_pool_size,
            connection_timeout_s=connection_timeout_s,
        )

    async def close(self) -> None:
        """Close the driver and drain all pooled connections.

        Call from the FastAPI lifespan shutdown hook. Safe to call on an
        already-closed client.

        Log events:
            neo4j_driver_closed  INFO
        """
        if self._driver is not None:
            await self._driver.close()
            self._driver = None
            logger.info("neo4j_driver_closed")

    def _get_driver(self) -> AsyncDriver:
        """Return the open driver, opening it lazily on first access."""
        if self._driver is None:
            self.open()
        return self._driver  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Connectivity probe
    # ------------------------------------------------------------------

    async def verify_connectivity(self) -> bool:
        """Probe Neo4j connectivity. Returns True on success, False on failure.

        Never raises — consistent with the startup probe pattern used across
        other services (api/services/health.py). Retries up to _MAX_RETRIES
        times with exponential backoff before returning False.

        Log events:
            neo4j_connectivity_ok       DEBUG   — on first successful probe
            neo4j_connectivity_retry    WARNING — attempt, wait_s, error
            neo4j_connectivity_failed   ERROR   — attempts, error
        """
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                await self._get_driver().verify_connectivity()
                logger.debug("neo4j_connectivity_ok")
                return True
            except Exception as exc:
                if attempt < _MAX_RETRIES:
                    wait_s = _BACKOFF_BASE_S * (2 ** (attempt - 1))
                    logger.warning(
                        "neo4j_connectivity_retry",
                        attempt=attempt,
                        wait_s=wait_s,
                        error=str(exc),
                    )
                    await asyncio.sleep(wait_s)
                else:
                    logger.error(
                        "neo4j_connectivity_failed",
                        attempts=attempt,
                        error=str(exc),
                    )
        return False

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def acquire_session(
        self, database: str | None = None
    ) -> AsyncIterator[AsyncSession]:
        """Async context manager yielding a Neo4j AsyncSession.

        The driver manages connection pooling internally. Callers should use
        session.execute_write() / session.execute_read() which handle
        TransientError retries internally (ACID semantics).

        For write operations with outer connection-level retry, prefer
        run_write_transaction() over using this context manager directly.

        Args:
            database: Optional Neo4j database name. None uses the server
                      default database.

        Yields:
            AsyncSession. Closed automatically on context exit.

        Log events:
            neo4j_session_acquired  DEBUG — database
        """
        driver = self._get_driver()
        async with driver.session(database=database) as session:
            logger.debug("neo4j_session_acquired", database=database or "default")
            yield session

    # ------------------------------------------------------------------
    # Write transaction with connection-level retry
    # ------------------------------------------------------------------

    async def run_write_transaction(
        self,
        tx_func: Callable[..., Coroutine[Any, Any, T]],
        database: str | None = None,
    ) -> T:
        """Execute a write transaction with retry on connection failures.

        session.execute_write() handles TransientError (deadlocks, leader
        switches) internally. This method adds an outer retry loop for
        ServiceUnavailable and SessionExpired — connection-level failures that
        the driver does not retry on its own.

        Args:
            tx_func:  Async callable with signature:
                          async def f(tx: AsyncManagedTransaction) -> T
                      Must be idempotent — it may be called more than once.
            database: Optional Neo4j database name.

        Returns:
            Whatever tx_func returns.

        Raises:
            ServiceUnavailable: After all _MAX_RETRIES attempts are exhausted.
            SessionExpired:     After all _MAX_RETRIES attempts are exhausted.
            All other neo4j exceptions propagate immediately without retry.

        Log events:
            neo4j_write_retry   WARNING — attempt, wait_s, error
            neo4j_write_failed  ERROR   — attempts, error
        """
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with self.acquire_session(database=database) as session:
                    return await session.execute_write(tx_func)  # type: ignore[return-value]
            except (ServiceUnavailable, SessionExpired) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    wait_s = _BACKOFF_BASE_S * (2 ** (attempt - 1))
                    logger.warning(
                        "neo4j_write_retry",
                        attempt=attempt,
                        wait_s=wait_s,
                        error=str(exc),
                    )
                    await asyncio.sleep(wait_s)
                else:
                    logger.error(
                        "neo4j_write_failed",
                        attempts=attempt,
                        error=str(exc),
                    )
        raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Process-level singleton
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_neo4j_client() -> Neo4jClient:
    """Return the process-level Neo4jClient singleton.

    The singleton is created on first call (lazy). The async driver is NOT
    opened here — the FastAPI lifespan hook must call client.open() explicitly
    before any write operations are attempted.

    Usage in api/main.py lifespan:
        client = get_neo4j_client()
        client.open()
        yield
        await client.close()
    """
    return Neo4jClient()
