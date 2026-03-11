"""
api/worker/config_sync.py — Worker-side credential synchronisation.

The API container receives credentials from the UI via POST /api/config/reload
and publishes them to Redis (CacheKey.config_credentials()).  The worker
container runs in a separate process with its own ``os.environ`` and cached
Settings singleton, so it must poll Redis for updated credentials.

``sync_credentials_from_redis()`` is called:
  - Once during worker startup (on_startup hook).
  - Before each job execution to pick up mid-run credential changes.

When new credentials are found in Redis, the function:
  1. Injects them into ``os.environ`` (so Pydantic Settings can read them).
  2. Calls ``reload_settings()`` to clear the LRU cache.
  3. If Neo4j credentials changed, resets the cached Neo4j client so the
     next ``get_neo4j_client()`` call creates a driver with fresh creds.

This is a no-op when Redis has no stored credentials (fresh deployment where
the .env already has correct values).

Security note (SKILL-D R-D5):
  Only key names are logged, never values.
"""

from __future__ import annotations

import os

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.config import reload_settings
from api.observability.logger import get_logger
from api.routers.models import RuntimeCredentials

logger = get_logger(__name__)

# Mapping from RuntimeCredentials fields to env-var names (mirrors config.py).
_FIELD_TO_ENVS: dict[str, list[str]] = {
    "neo4j_uri": ["NEO4J_URI", "NEO4J_DEV_URI"],
    "neo4j_user": ["NEO4J_USERNAME", "NEO4J_DEV_USER"],
    "neo4j_password": ["NEO4J_PASSWORD", "NEO4J_DEV_PASSWORD"],
    "openrouter_api_key": ["OPENROUTER_API_KEY"],
}

_NEO4J_FIELDS = {"neo4j_uri", "neo4j_user", "neo4j_password"}


async def sync_credentials_from_redis(
    ctx: dict | None = None,
) -> bool:
    """Pull credentials from Redis and inject into this process's environment.

    Args:
        ctx: Optional ARQ worker context dict.  When provided and Neo4j
             credentials changed, the cached Neo4j client is closed and
             reopened with the new credentials, and ``ctx["neo4j_client"]``
             is updated in-place so subsequent jobs use the fresh driver.

    Returns True if any credentials were updated, False otherwise.

    Log events:
        worker_credentials_synced       INFO    — keys (names only)
        worker_credentials_noop         DEBUG   — no credentials in Redis
        worker_credentials_error        WARNING — Redis read failed
        worker_neo4j_client_reset       INFO    — Neo4j driver reopened
    """
    try:
        cache = get_cache_client()
        creds = await cache.get(
            CacheKey.config_credentials(), model=RuntimeCredentials
        )
        if creds is None:
            logger.debug("worker_credentials_noop")
            return False

        injected: list[str] = []
        for field_name, env_names in _FIELD_TO_ENVS.items():
            value = getattr(creds, field_name, None)
            if value is not None and value.strip():
                for env_name in env_names:
                    os.environ[env_name] = value.strip()
                injected.append(field_name)

        if not injected:
            logger.debug("worker_credentials_noop")
            return False

        reload_settings()
        logger.info(
            "worker_credentials_synced",
            keys=[_FIELD_TO_ENVS[f][0] for f in injected],
        )

        # If Neo4j credentials changed, reset the driver so the next
        # call to get_neo4j_client() picks up the new URI/user/password.
        neo4j_changed = bool(_NEO4J_FIELDS & set(injected))
        if neo4j_changed:
            from api.graph.client import get_neo4j_client, reset_neo4j_client

            await reset_neo4j_client()

            # Reopen the driver and update ARQ context if available.
            if ctx is not None:
                client = get_neo4j_client()
                client.open()
                ctx["neo4j_client"] = client
                logger.info("worker_neo4j_client_reset")

        return True
    except Exception as exc:
        logger.warning("worker_credentials_error", error=str(exc))
        return False
