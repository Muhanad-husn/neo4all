"""
api/cache/keys.py — Deterministic cache key builders (SKILL-D R-D7).

All cache keys MUST be constructed through the static methods on CacheKey.
String concatenation for key construction is banned — CLAUDE.md §15.

Key format (matches SKILL-D R-D8 examples):
  schema:{run_id}
  manifest:{run_id}:{doc_id}
  gq:{run_id}:{query_hash}
  chunk:{chunk_id}
  candidates:{run_id}:{detection_hash}

Why static methods instead of f-strings at call sites?
  - Ensures every caller uses the same key format (no drift).
  - Provides a single place to change key structure without grep-hunting.
  - Matches the project's "typed builders for deterministic IDs" philosophy.

Usage:
    from api.cache.keys import CacheKey

    key = CacheKey.schema(run_id=run_id)
    key = CacheKey.manifest(run_id=run_id, doc_id=doc_id)
    key = CacheKey.graph_query(run_id=run_id, query_hash=h)
    key = CacheKey.chunk(chunk_id=chunk_id)
    key = CacheKey.candidates(run_id=run_id, detection_hash=dh)
"""


class CacheKey:
    """Namespace for deterministic cache key derivation methods.

    All methods are static — CacheKey is never instantiated.
    """

    @staticmethod
    def schema(run_id: str) -> str:
        """Key for a locked domain schema (no TTL — immutable after lock).

        Pattern: schema:{run_id}
        """
        return f"schema:{run_id}"

    @staticmethod
    def manifest(run_id: str, doc_id: str) -> str:
        """Key for a document manifest within a run.

        Pattern: manifest:{run_id}:{doc_id}
        Invalidated on re-ingestion of the same document.
        """
        return f"manifest:{run_id}:{doc_id}"

    @staticmethod
    def graph_query(run_id: str, query_hash: str) -> str:
        """Key for a graph reader query result (node counts, type lists, etc.).

        Pattern: gq:{run_id}:{query_hash}

        query_hash must be a deterministic hash of the query parameters, NOT
        the raw query string (which could contain sensitive data).
        """
        return f"gq:{run_id}:{query_hash}"

    @staticmethod
    def chunk(chunk_id: str) -> str:
        """Key for chunk text content.

        Pattern: chunk:{chunk_id}
        Chunks are immutable after ingestion — never explicitly invalidated.
        """
        return f"chunk:{chunk_id}"

    @staticmethod
    def candidates(run_id: str, detection_hash: str) -> str:
        """Key for candidate detection results within a run.

        Pattern: candidates:{run_id}:{detection_hash}

        detection_hash must be a deterministic hash of the detection parameters
        (schema_version, detection rules, etc.).
        """
        return f"candidates:{run_id}:{detection_hash}"

    @staticmethod
    def job_status(run_id: str, chunk_id: str) -> str:
        """Key for per-chunk extraction job status.

        Pattern: job:{run_id}:{chunk_id}
        TTL: 86400 s (24 hours — enough to span any extraction run).
        Written by api/worker/jobs.py; read by api/routers/monitoring.py
        (GET /api/monitoring/jobs/{run_id}).
        """
        return f"job:{run_id}:{chunk_id}"

    @staticmethod
    def run_prefix(run_id: str) -> str:
        """Return the key prefix shared by all keys scoped to a run_id.

        Used with CacheClient.invalidate_prefix() to evict all run-scoped keys
        after an Agent-C graph mutation (SKILL-D R-D10).

        Note: schema:{run_id} keys are deliberately excluded from bulk
        invalidation because the schema is immutable after Phase 1 lock.
        """
        return f"run:{run_id}:"
