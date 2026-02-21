"""
api/cache/config.py — TTL defaults for each cacheable data type.

Values are in seconds. None means the key does not expire (the entry lives for
the lifetime of the Run and is invalidated explicitly, not by TTL).

Source: SKILL-D R-D8 table.

Usage:
    from api.cache.config import CACHE_TTL
    await cache.set(key, value, ttl=CACHE_TTL.manifest)
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class _CacheTTL:
    """TTL configuration per data type. Immutable after module import."""

    # Locked domain schema — immutable after Phase 1 lock; never expires.
    # Invalidation: never (schema is write-once per run_id).
    schema: int | None = None

    # Document manifest — invalidated on re-ingestion of the same doc.
    manifest: int = 3_600  # 1 hour

    # Graph reader queries (node counts, type lists, etc.) — stale after
    # any graph write within the run.
    graph_query: int = 300  # 5 minutes

    # Chunk text keyed by chunk_id — chunks are immutable after ingestion.
    chunk: int = 1_800  # 30 minutes

    # Candidate detection results — invalidated on graph write or manual
    # regeneration trigger.
    candidates: int = 300  # 5 minutes


CACHE_TTL: _CacheTTL = _CacheTTL()
