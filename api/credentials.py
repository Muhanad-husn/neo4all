"""
api/credentials.py — Single source of truth for runtime credentials.

Provides a unified resolution point for Neo4j and OpenRouter credentials.
All credential consumers read from ``get_credentials()`` instead of
accessing Settings or os.environ directly for credential fields.

Resolution chain
-----------------
  1. In-memory override (populated from Redis via ``refresh_credentials()``)
  2. Fallback to ``Settings`` (env vars / ``.env`` file)

Thread safety
-------------
The module-level ``_override`` is protected by a ``threading.Lock`` so
that ``get_credentials()`` (synchronous, called from sync driver code)
and ``refresh_credentials()`` (async, called at startup / before jobs)
do not race.

Sensitive data (SKILL-D R-D5)
------------------------------
Credential values are never logged.  Only key names and boolean flags
(e.g. "override_active") appear in structured log events.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from api.observability.logger import get_logger
from api.routers.models import RuntimeCredentials

logger = get_logger(__name__)


@dataclass(frozen=True)
class ResolvedCredentials:
    """Immutable snapshot of resolved credentials.

    Returned by ``get_credentials()``.  Consumers use this instead of
    reading ``Settings`` fields directly for credential values.
    """

    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    openrouter_api_key: str


_lock = threading.Lock()
_override: RuntimeCredentials | None = None


def get_credentials() -> ResolvedCredentials:
    """Return resolved credentials (synchronous).

    Checks the in-memory override first (populated from Redis).  For any
    field that is None or blank in the override, falls back to Settings.
    If no override exists, all fields come from Settings.

    Returns:
        ResolvedCredentials with all four credential fields populated.
    """
    from api.config import get_settings

    with _lock:
        ovr = _override

    s = get_settings()

    if ovr is not None:
        return ResolvedCredentials(
            neo4j_uri=(ovr.neo4j_uri.strip() if ovr.neo4j_uri and ovr.neo4j_uri.strip() else s.NEO4J_DEV_URI),
            neo4j_user=(ovr.neo4j_user.strip() if ovr.neo4j_user and ovr.neo4j_user.strip() else s.NEO4J_DEV_USER),
            neo4j_password=(ovr.neo4j_password.strip() if ovr.neo4j_password and ovr.neo4j_password.strip() else s.NEO4J_DEV_PASSWORD),
            openrouter_api_key=(ovr.openrouter_api_key.strip() if ovr.openrouter_api_key and ovr.openrouter_api_key.strip() else s.OPENROUTER_API_KEY),
        )

    return ResolvedCredentials(
        neo4j_uri=s.NEO4J_DEV_URI,
        neo4j_user=s.NEO4J_DEV_USER,
        neo4j_password=s.NEO4J_DEV_PASSWORD,
        openrouter_api_key=s.OPENROUTER_API_KEY,
    )


async def refresh_credentials() -> bool:
    """Read RuntimeCredentials from Redis and store as in-memory override.

    Called at startup and before each worker job to pick up credentials
    published by the UI via ``POST /api/config/reload``.

    Returns:
        True if credentials were loaded from Redis, False otherwise.

    Log events:
        credentials_refreshed       INFO    — override now active
        credentials_refresh_noop    DEBUG   — no credentials in Redis
        credentials_refresh_error   WARNING — Redis read failed (fallback to env)
    """
    global _override

    try:
        from api.cache.client import get_cache_client
        from api.cache.keys import CacheKey

        cache = get_cache_client()
        creds = await cache.get(
            CacheKey.config_credentials(), model=RuntimeCredentials
        )

        if creds is None:
            logger.debug("credentials_refresh_noop")
            return False

        with _lock:
            _override = creds

        logger.info("credentials_refreshed", override_active=True)
        return True

    except Exception as exc:
        logger.warning("credentials_refresh_error", error=str(exc))
        return False


async def publish_credentials(creds: RuntimeCredentials) -> bool:
    """Write credentials to Redis and refresh the local override.

    Called by the config reload endpoint when the UI submits new credentials.

    Args:
        creds: RuntimeCredentials to publish.

    Returns:
        True if publish and refresh succeeded, False otherwise.

    Log events:
        credentials_published       INFO    — written to Redis
        credentials_publish_failed  WARNING — Redis write failed
    """
    try:
        from api.cache.client import get_cache_client
        from api.cache.keys import CacheKey

        cache = get_cache_client()
        stored = await cache.set(CacheKey.config_credentials(), creds)

        if stored:
            logger.info("credentials_published")
            await refresh_credentials()
            return True

        logger.warning("credentials_publish_failed")
        return False

    except Exception as exc:
        logger.warning("credentials_publish_failed", error=str(exc))
        return False


def clear_credential_override() -> None:
    """Clear the in-memory credential override.  For tests only."""
    global _override

    with _lock:
        _override = None
