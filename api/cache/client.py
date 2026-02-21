"""
api/cache/client.py — Async Redis cache abstraction (SKILL-D R-D6).

All cache operations flow through CacheClient. No module may import or call
redis directly outside this file — CLAUDE.md §15.

Graceful degradation (SKILL-D R-D9)
-------------------------------------
A cache miss is a normal operational event, not an error. When Redis is
unavailable (connection refused, timeout, etc.) each method:
  - Logs at WARNING level with structured fields.
  - Returns None / False / 0 so the caller falls through to the authoritative
    source (Neo4j, S3, Qdrant).
  - Never raises. Never propagates a Redis exception to the caller.

The application continues without caching. Data correctness is preserved
because callers always fall back to the authoritative source on a None return.

Typed serialisation
-------------------
get() accepts a Pydantic model class. Stored values are JSON-serialised via
model.model_dump_json(). Retrieved values are deserialised via
model.model_validate_json(). This ensures type safety at the cache boundary
and prevents raw dict leakage (SKILL-A R-A3).

Usage:
    from api.cache.client import get_cache_client
    from api.cache.keys import CacheKey
    from api.cache.config import CACHE_TTL

    cache = get_cache_client()
    key = CacheKey.schema(run_id=run_id)

    schema = await cache.get(key, model=SchemaVersion)
    if schema is None:
        schema = await fetch_from_neo4j(run_id)
        await cache.set(key, schema, ttl=CACHE_TTL.schema)
"""

from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel

from api.observability.logger import get_logger

logger = get_logger(__name__)

try:
    import redis.asyncio as aioredis
    _REDIS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _REDIS_AVAILABLE = False

T = TypeVar("T", bound=BaseModel)


class CacheClient:
    """Async Redis wrapper with typed get/set and graceful degradation.

    Lazily initialises the Redis connection on first use so that the class can
    be instantiated without an active Redis server (useful in unit tests that
    do not touch the cache).
    """

    def __init__(self, redis_url: str | None = None) -> None:
        """Initialise CacheClient.

        Args:
            redis_url: Redis connection string. Defaults to the REDIS_URL
                       environment variable via api.config.get_settings().
        """
        self._redis_url: str | None = redis_url
        self._client: aioredis.Redis | None = None  # type: ignore[name-defined]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_redis_url(self) -> str:
        if self._redis_url:
            return self._redis_url
        from api.config import get_settings
        return get_settings().REDIS_URL

    def _get_client(self) -> "aioredis.Redis":  # type: ignore[name-defined]
        """Return the Redis client, creating it on first call."""
        if self._client is None:
            if not _REDIS_AVAILABLE:
                raise RuntimeError("redis package not installed")
            self._client = aioredis.from_url(
                self._get_redis_url(),
                decode_responses=True,
            )
        return self._client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self, key: str, *, model: type[T]) -> T | None:
        """Retrieve and deserialise a cached value.

        Args:
            key:   Cache key (built via CacheKey.*).
            model: Pydantic model class used to deserialise the stored JSON.

        Returns:
            Deserialised model instance on cache hit, None on miss or error.

        Log events:
            cache_hit     DEBUG — key
            cache_miss    DEBUG — key
            cache_get_error WARNING — key, error
        """
        try:
            client = self._get_client()
            raw: str | None = await client.get(key)
            if raw is None:
                logger.debug("cache_miss", key=key)
                return None
            logger.debug("cache_hit", key=key)
            return model.model_validate_json(raw)
        except Exception as exc:
            logger.warning("cache_get_error", key=key, error=str(exc))
            return None

    async def set(
        self,
        key: str,
        value: BaseModel,
        *,
        ttl: int | None = None,
    ) -> bool:
        """Serialise and store a Pydantic model in Redis.

        Args:
            key:   Cache key (built via CacheKey.*).
            value: Pydantic model instance to store.
            ttl:   Time-to-live in seconds. None means no expiry (key lives
                   until explicitly deleted or Redis is restarted).

        Returns:
            True on success, False on error (Redis down, serialisation failure).

        Log events:
            cache_set       DEBUG — key, ttl
            cache_set_error WARNING — key, error
        """
        try:
            client = self._get_client()
            payload = value.model_dump_json()
            if ttl is not None:
                await client.setex(key, ttl, payload)
            else:
                await client.set(key, payload)
            logger.debug("cache_set", key=key, ttl=ttl)
            return True
        except Exception as exc:
            logger.warning("cache_set_error", key=key, error=str(exc))
            return False

    async def delete(self, key: str) -> bool:
        """Delete a single key.

        Args:
            key: Cache key to remove.

        Returns:
            True if the key was deleted (or did not exist), False on error.

        Log events:
            cache_deleted       DEBUG — key
            cache_delete_error  WARNING — key, error
        """
        try:
            client = self._get_client()
            await client.delete(key)
            logger.debug("cache_deleted", key=key)
            return True
        except Exception as exc:
            logger.warning("cache_delete_error", key=key, error=str(exc))
            return False

    async def invalidate_prefix(self, prefix: str) -> int:
        """Delete all keys matching a prefix (uses SCAN, not KEYS).

        SCAN is used instead of KEYS to avoid blocking the Redis event loop on
        large key spaces.

        Args:
            prefix: Key prefix to match. E.g. "run:{run_id}:".

        Returns:
            Number of keys deleted, or 0 on error.

        Log events:
            cache_prefix_invalidated  INFO    — prefix, count
            cache_prefix_error        WARNING — prefix, error
        """
        try:
            client = self._get_client()
            count = 0
            async for key in client.scan_iter(match=f"{prefix}*"):
                await client.delete(key)
                count += 1
            logger.info("cache_prefix_invalidated", prefix=prefix, count=count)
            return count
        except Exception as exc:
            logger.warning("cache_prefix_error", prefix=prefix, error=str(exc))
            return 0

    async def ping(self) -> bool:
        """Check Redis connectivity.

        Used by the monitoring health endpoint. Returns False (not an error)
        if Redis is unreachable — consistent with graceful degradation policy.
        """
        try:
            client = self._get_client()
            await client.ping()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Process-level singleton
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_cache_client() -> CacheClient:
    """Return the process-level CacheClient singleton.

    The singleton is created on first call (lazy). The REDIS_URL setting is
    resolved at that point, so startup validation has already run.
    """
    return CacheClient()
