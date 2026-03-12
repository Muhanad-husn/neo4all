"""
api/worker/config_sync.py — Worker-side credential synchronisation.

The API container receives credentials from the UI via POST /api/config/reload
and publishes them to Redis.  The worker container runs in a separate process,
so it must call ``refresh_credentials()`` to pick up updated credentials from
Redis into the in-memory override used by ``get_credentials()``.

``sync_credentials_from_redis()`` is called:
  - Once during worker startup (on_startup hook).
  - Before each job execution to pick up mid-run credential changes.

When new credentials are found in Redis, the function refreshes the
in-memory credential override and, if Neo4j credentials changed, resets
the cached Neo4j client so the next call creates a driver with fresh creds.

Security note (SKILL-D R-D5):
  Only key names are logged, never values.
"""

from __future__ import annotations

from api.credentials import get_credentials, refresh_credentials
from api.observability.logger import get_logger

logger = get_logger(__name__)


async def sync_credentials_from_redis(
    ctx: dict | None = None,
) -> bool:
    """Pull credentials from Redis into the in-memory credential override.

    Args:
        ctx: Optional ARQ worker context dict.  When provided and Neo4j
             credentials changed, the cached Neo4j client is closed and
             reopened with the new credentials, and ``ctx["neo4j_client"]``
             is updated in-place so subsequent jobs use the fresh driver.

    Returns True if credentials were refreshed from Redis, False otherwise.

    Log events:
        worker_credentials_synced       INFO    — refresh succeeded
        worker_credentials_noop         DEBUG   — no credentials in Redis
        worker_credentials_error        WARNING — Redis read failed
        worker_neo4j_client_reset       INFO    — Neo4j driver reopened
    """
    try:
        # Capture previous Neo4j credentials to detect changes.
        prev = get_credentials()

        refreshed = await refresh_credentials()
        if not refreshed:
            logger.debug("worker_credentials_noop")
            return False

        logger.info("worker_credentials_synced")

        # Check if Neo4j credentials changed — if so, reset the driver.
        current = get_credentials()
        neo4j_changed = (
            prev.neo4j_uri != current.neo4j_uri
            or prev.neo4j_user != current.neo4j_user
            or prev.neo4j_password != current.neo4j_password
        )

        if neo4j_changed:
            from api.graph.client import get_neo4j_client, reset_neo4j_client

            await reset_neo4j_client()

            if ctx is not None:
                client = get_neo4j_client()
                client.open()
                ctx["neo4j_client"] = client
                logger.info("worker_neo4j_client_reset")

        return True
    except Exception as exc:
        logger.warning("worker_credentials_error", error=str(exc))
        return False
