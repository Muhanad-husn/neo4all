"""api/common/arq_pool.py — Shared ARQ Redis connection pool.

Consolidates the duplicate _get_arq_pool() from api/routers/extraction.py
and api/routers/monitoring.py (SKILL-B R-B7).

The pool is a process-level lazy singleton shared across all FastAPI
route handlers.  Created on first call to avoid importing ARQ at module
load time when env vars may not yet be present.

Sensitive data (SKILL-D R-D5)
------------------------------
REDIS_URL is never logged in full — only the host fragment after ``@``.
"""

from __future__ import annotations

from typing import Any

from api.observability.logger import get_logger

logger = get_logger(__name__)

_arq_pool: Any = None

QUEUE_NAME: str = "arq:queue"  # must match api/worker/entry.WorkerSettings.queue_name


async def get_arq_pool() -> Any:
    """Return (or create) the process-level ARQ Redis pool.

    Uses a module-level variable so a single connection pool is shared across
    all requests in the process.  Created lazily on first call to avoid
    importing ARQ at module load time when env vars may not yet be present.

    Raises:
        ValidationError: If REDIS_URL is absent from Settings (fail-closed).

    Log events:
        arq_pool_created  INFO — redis_host
    """
    global _arq_pool
    if _arq_pool is None:
        from arq import create_pool as arq_create_pool
        from arq.connections import RedisSettings

        from api.config import get_settings

        settings = get_settings()
        redis_url = settings.REDIS_URL
        _arq_pool = await arq_create_pool(
            RedisSettings.from_dsn(redis_url),
            default_queue_name=QUEUE_NAME,
        )
        # Log only the host fragment — never the full URL (SKILL-D R-D5).
        safe_host = redis_url.split("@")[-1] if "@" in redis_url else redis_url
        logger.info("arq_pool_created", redis_host=safe_host)

    return _arq_pool
