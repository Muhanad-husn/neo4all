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
    def proposal(proposal_id: str) -> str:
        """Key for a single ProposalPacket (no TTL — proposals live for run lifetime).

        Pattern: proposal:{proposal_id}
        """
        return f"proposal:{proposal_id}"

    @staticmethod
    def proposals_index(run_id: str) -> str:
        """Key for the ordered list of proposal_ids belonging to a run.

        Pattern: proposals_index:{run_id}
        Maintained by ProposalService as an append-only Redis list of proposal_ids.
        Used by list_for_run() to avoid S3 prefix listing on every request.
        """
        return f"proposals_index:{run_id}"

    @staticmethod
    def approval(approval_id: str) -> str:
        """Key for an ApprovalRecord issued by the Approval Gate.

        Pattern: approval:{approval_id}
        No TTL — approval records are immutable for run lifetime.
        Written by api/routers/approvals.py; read by api/agents/execution.py.
        """
        return f"approval:{approval_id}"

    @staticmethod
    def graph_query_prefix(run_id: str) -> str:
        """Prefix for all graph-reader cache keys belonging to a run.

        Pattern: gq:{run_id}:

        Passed to CacheClient.invalidate_prefix() by Agent-C after a graph
        mutation to evict stale graph query results (SKILL-D R-D10).
        """
        return f"gq:{run_id}:"

    @staticmethod
    def candidates_prefix(run_id: str) -> str:
        """Prefix for all candidate-detection cache keys belonging to a run.

        Pattern: candidates:{run_id}:

        Passed to CacheClient.invalidate_prefix() by Agent-C after a graph
        mutation to evict stale candidate results (SKILL-D R-D10).
        """
        return f"candidates:{run_id}:"

    @staticmethod
    def confirmation(token: str) -> str:
        """Key for a pending two-phase approval confirmation record.

        Pattern: confirm:{token}
        TTL: 3600 s (1 hour — enough time for the operator to complete phase 2).
        Written by api/routers/approvals.py (phase 1 of high-risk approval);
        read and deleted on confirm (phase 2).
        """
        return f"confirm:{token}"

    @staticmethod
    def agent_job(run_id: str, candidate_id: str) -> str:
        """Key for per-candidate agent pipeline job status.

        Pattern: agent_job:{run_id}:{candidate_id}
        TTL: 86400 s (24 hours — mirrors extraction job status).
        Written by api/worker/jobs.py (evidence/retrieval/proposal jobs);
        read by api/routers/monitoring.py (GET /api/monitoring/agents/{run_id}).
        """
        return f"agent_job:{run_id}:{candidate_id}"

    @staticmethod
    def run_prefix(run_id: str) -> str:
        """Return the key prefix shared by all keys scoped to a run_id.

        Used with CacheClient.invalidate_prefix() to evict all run-scoped keys
        after an Agent-C graph mutation (SKILL-D R-D10).

        Note: schema:{run_id} keys are deliberately excluded from bulk
        invalidation because the schema is immutable after Phase 1 lock.
        """
        return f"run:{run_id}:"
