"""
tests/unit/test_dry_run.py — Dry-run mode behaviour (SPEC-08 S-08.1).

Validates that when DRY_RUN=True, Agent-C:
  - Skips all graph mutations (no Neo4j writes).
  - Still performs pre-execution validation (approval, schema, proposal state).
  - Still generates diffs and proposals (Diff Builder, Proposal Service).
  - Logs the diff payload via structlog.
  - Records an AuditRecord with outcome=applied, steps_applied=0, dry_run=True.
  - Executes normally when DRY_RUN=False.

Also validates:
  - DRY_RUN flag defaults to False in api/config.py Settings.
  - GET /api/monitoring/cache returns valid CacheStatsResponse (SPEC-08 S-08.3).
  - Cache hit ratio is calculated correctly from hit/miss counters.

No network, no LLM, no Neo4j. All external services are mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.agents.execution import (
    ApprovalRecord,
    ExecutionAgent,
)
from api.audit.models import AuditOutcome, AuditRecord
from api.diff.models import DiffOperation, DiffPlan, DiffStep
from api.observability.metrics import get_metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROPOSAL_ID = "a" * 64
_SCHEMA_VERSION = "sv_test_001"
_RUN_ID = "run_dry"
_CANDIDATE_ID = "cand_dry"
_APPROVAL_ID = "approval_dry_001"
_ACTOR = "test_operator"


def _make_diff(steps: tuple[DiffStep, ...] | None = None) -> DiffPlan:
    """Create a valid DiffPlan with default or custom steps."""
    if steps is None:
        steps = (
            DiffStep(
                operation=DiffOperation.update_node,
                dedupe_key="Person|Alice|sv1",
                properties_set={"name": "Alice Updated"},
            ),
        )
    return DiffPlan(
        proposal_id=_PROPOSAL_ID,
        run_id=_RUN_ID,
        candidate_id=_CANDIDATE_ID,
        schema_version=_SCHEMA_VERSION,
        steps=steps,
    )


def _make_approval() -> ApprovalRecord:
    """Create a matching ApprovalRecord for the default diff."""
    return ApprovalRecord(
        approval_id=_APPROVAL_ID,
        proposal_id=_PROPOSAL_ID,
        run_id=_RUN_ID,
        actor=_ACTOR,
        is_confirmed=True,
    )


def _stub_agent(dry_run: bool) -> ExecutionAgent:
    """Build an ExecutionAgent with fully mocked dependencies."""
    neo4j = MagicMock()
    schema_service = AsyncMock()
    proposal_service = AsyncMock()
    audit_writer = AsyncMock()
    cache = AsyncMock()

    # Pre-execution validation mocks
    cache.get = AsyncMock(return_value=_make_approval())
    schema_service.get_current = AsyncMock(
        return_value=MagicMock(version_hash=_SCHEMA_VERSION)
    )
    proposal_service.get_for_run = AsyncMock(
        return_value=MagicMock(state="approved")
    )

    agent = ExecutionAgent(
        neo4j_client=neo4j,
        schema_service=schema_service,
        proposal_service=proposal_service,
        audit_writer=audit_writer,
        cache=cache,
    )
    return agent


# ---------------------------------------------------------------------------
# Tests — DRY_RUN=True
# ---------------------------------------------------------------------------


class TestDryRunEnabled:
    """When DRY_RUN=True, Agent-C must skip graph mutations entirely."""

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_settings")
    async def test_no_graph_writes(self, mock_settings: MagicMock) -> None:
        """No Neo4j write transaction should be called."""
        mock_settings.return_value = MagicMock(DRY_RUN=True)
        agent = _stub_agent(dry_run=True)
        diff = _make_diff()

        record = await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        # The neo4j client should NOT have run_write_transaction called
        agent._neo4j.run_write_transaction.assert_not_called()
        assert isinstance(record, AuditRecord)

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_settings")
    async def test_audit_record_outcome(self, mock_settings: MagicMock) -> None:
        """AuditRecord should show applied outcome with zero steps and dry_run flag."""
        mock_settings.return_value = MagicMock(DRY_RUN=True)
        agent = _stub_agent(dry_run=True)
        diff = _make_diff()

        record = await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        assert record.outcome == AuditOutcome.applied
        assert record.steps_applied == 0
        assert record.error_detail == {"dry_run": True}

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_settings")
    async def test_audit_writer_called(self, mock_settings: MagicMock) -> None:
        """AuditWriter.append must still be called in dry-run mode."""
        mock_settings.return_value = MagicMock(DRY_RUN=True)
        agent = _stub_agent(dry_run=True)
        diff = _make_diff()

        await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        agent._auditor.append.assert_called_once()

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_settings")
    async def test_pre_validation_still_runs(self, mock_settings: MagicMock) -> None:
        """Pre-execution validation (approval lookup) must still happen."""
        mock_settings.return_value = MagicMock(DRY_RUN=True)
        agent = _stub_agent(dry_run=True)
        diff = _make_diff()

        await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        # Cache.get is called for approval lookup during pre-validation
        agent._cache.get.assert_called_once()

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_settings")
    async def test_rejected_on_bad_approval_even_in_dry_run(
        self, mock_settings: MagicMock
    ) -> None:
        """Pre-validation failure rejects even when DRY_RUN=True."""
        mock_settings.return_value = MagicMock(DRY_RUN=True)
        agent = _stub_agent(dry_run=True)
        # Override cache to return None (approval not found)
        agent._cache.get = AsyncMock(return_value=None)
        diff = _make_diff()

        record = await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        assert record.outcome == AuditOutcome.rejected

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_settings")
    async def test_no_cache_invalidation(self, mock_settings: MagicMock) -> None:
        """Cache invalidation must NOT happen in dry-run mode (no mutation occurred)."""
        mock_settings.return_value = MagicMock(DRY_RUN=True)
        agent = _stub_agent(dry_run=True)
        diff = _make_diff()

        await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        agent._cache.invalidate_prefix.assert_not_called()

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_settings")
    async def test_multi_step_diff_skipped(self, mock_settings: MagicMock) -> None:
        """A multi-step diff should still produce steps_applied=0 in dry-run."""
        mock_settings.return_value = MagicMock(DRY_RUN=True)
        agent = _stub_agent(dry_run=True)
        steps = (
            DiffStep(
                operation=DiffOperation.create_node,
                dedupe_key="Person|Bob|sv1",
                node_labels=("Person",),
                properties_set={"name": "Bob"},
            ),
            DiffStep(
                operation=DiffOperation.create_edge,
                dedupe_key="KNOWS|Bob|Alice|sv1",
                start_dedupe_key="Person|Bob|sv1",
                end_dedupe_key="Person|Alice|sv1",
                rel_type="KNOWS",
            ),
        )
        diff = _make_diff(steps=steps)

        record = await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        assert record.steps_applied == 0
        assert record.error_detail == {"dry_run": True}


# ---------------------------------------------------------------------------
# Tests — DRY_RUN=False (default behaviour preserved)
# ---------------------------------------------------------------------------


class TestDryRunDisabled:
    """When DRY_RUN=False (default), Agent-C must proceed with graph mutations."""

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_settings")
    async def test_graph_writes_proceed(self, mock_settings: MagicMock) -> None:
        """Neo4j write transactions should be called when not in dry-run."""
        mock_settings.return_value = MagicMock(DRY_RUN=False)
        agent = _stub_agent(dry_run=False)

        # Mock _apply_step to succeed without actual Neo4j
        agent._apply_step = AsyncMock()  # type: ignore[method-assign]
        agent._check_invariant = AsyncMock()  # type: ignore[method-assign]
        agent._invalidate_run_cache = AsyncMock()  # type: ignore[method-assign]
        diff = _make_diff()

        record = await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        assert record.outcome == AuditOutcome.applied
        assert record.steps_applied == 1
        assert record.error_detail == {}

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_settings")
    async def test_cache_invalidation_on_success(self, mock_settings: MagicMock) -> None:
        """Cache invalidation should happen after successful mutation."""
        mock_settings.return_value = MagicMock(DRY_RUN=False)
        agent = _stub_agent(dry_run=False)
        agent._apply_step = AsyncMock()  # type: ignore[method-assign]
        agent._check_invariant = AsyncMock()  # type: ignore[method-assign]
        agent._invalidate_run_cache = AsyncMock()  # type: ignore[method-assign]
        diff = _make_diff()

        await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        agent._invalidate_run_cache.assert_called_once_with(_RUN_ID)


# ---------------------------------------------------------------------------
# Tests — DRY_RUN config default
# ---------------------------------------------------------------------------


class TestDryRunFlagDefault:
    """Verify that DRY_RUN defaults to False in Settings."""

    def test_dry_run_flag_default(self) -> None:
        """DRY_RUN must default to False when the env var is unset."""
        from api.config import Settings

        # Build Settings with only the required fields, DRY_RUN absent.
        settings = Settings(
            NEO4J_DEV_URI="neo4j+s://test.databases.neo4j.io",
            NEO4J_DEV_USER="neo4j",
            NEO4J_DEV_PASSWORD="test_password",
            OPENROUTER_API_KEY="test_key",
            S3_ENDPOINT_URL="http://localhost:9000",
            S3_ACCESS_KEY_ID="minioadmin",
            S3_SECRET_ACCESS_KEY="minioadmin",
            S3_BUCKET_NAME="test-bucket",
            REDIS_URL="redis://localhost:6379",
        )
        assert settings.DRY_RUN is False


# ---------------------------------------------------------------------------
# Tests — DRY_RUN=True skips mutations (mock Neo4j client verification)
# ---------------------------------------------------------------------------


class TestDryRunSkipsMutations:
    """Verify that DRY_RUN=True prevents any Neo4j write calls."""

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_settings")
    async def test_dry_run_skips_mutations(self, mock_settings: MagicMock) -> None:
        """When DRY_RUN=True, no write transaction methods on Neo4jClient are invoked."""
        mock_settings.return_value = MagicMock(DRY_RUN=True)
        agent = _stub_agent(dry_run=True)
        diff = _make_diff()

        record = await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        # Verify zero graph mutations occurred
        agent._neo4j.run_write_transaction.assert_not_called()
        # Verify the record reflects dry-run
        assert record.outcome == AuditOutcome.applied
        assert record.steps_applied == 0
        assert record.error_detail.get("dry_run") is True


# ---------------------------------------------------------------------------
# Tests — DRY_RUN=True still generates diffs
# ---------------------------------------------------------------------------


class TestDryRunGeneratesDiffs:
    """Verify diffs and proposals are still generated in dry-run mode."""

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_artifacts_service")
    @patch("api.agents.execution.get_settings")
    async def test_dry_run_generates_diffs(
        self, mock_settings: MagicMock, mock_get_artifacts: MagicMock
    ) -> None:
        """Diff Builder and proposal validation still execute; S3 artifact stored."""
        mock_settings.return_value = MagicMock(DRY_RUN=True)
        mock_artifacts = AsyncMock()
        mock_get_artifacts.return_value = mock_artifacts

        agent = _stub_agent(dry_run=True)
        diff = _make_diff()

        record = await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        # Pre-execution validation (proposal service) was called
        agent._proposals.get_for_run.assert_called_once_with(_RUN_ID, _PROPOSAL_ID)
        # Schema service was called to verify schema version
        agent._schema.get_current.assert_called_once_with(_RUN_ID)
        # The diff was persisted to S3 for review
        mock_artifacts.store_json.assert_called_once()
        s3_key_arg = mock_artifacts.store_json.call_args[0][0]
        assert s3_key_arg == f"{_RUN_ID}/dry-run/{diff.diff_id}.json"
        # AuditRecord was written
        agent._auditor.append.assert_called_once()
        assert record.outcome == AuditOutcome.applied


# ---------------------------------------------------------------------------
# Tests — DRY_RUN=True logs proposal via structlog
# ---------------------------------------------------------------------------


class TestDryRunLogsProposals:
    """Verify that dry-run mode emits structured log events."""

    @pytest.mark.asyncio
    @patch("api.agents.execution.get_artifacts_service")
    @patch("api.agents.execution.logger")
    @patch("api.agents.execution.get_settings")
    async def test_dry_run_logs_proposals(
        self,
        mock_settings: MagicMock,
        mock_logger: MagicMock,
        mock_get_artifacts: MagicMock,
    ) -> None:
        """A 'dry_run_skipped' log event must be emitted with proposal payload."""
        mock_settings.return_value = MagicMock(DRY_RUN=True)
        mock_get_artifacts.return_value = AsyncMock()

        agent = _stub_agent(dry_run=True)
        steps = (
            DiffStep(
                operation=DiffOperation.update_node,
                dedupe_key="Person|Alice|sv1",
                properties_set={"name": "Alice Updated"},
            ),
        )
        diff = _make_diff(steps=steps)

        await agent.execute(diff, _APPROVAL_ID, _ACTOR)

        # Verify the structured log event was emitted
        dry_run_calls = [
            c for c in mock_logger.info.call_args_list if c.args[0] == "dry_run_skipped"
        ]
        assert len(dry_run_calls) == 1
        call_kwargs = dry_run_calls[0].kwargs
        assert call_kwargs["proposal_id"] == _PROPOSAL_ID
        assert call_kwargs["run_id"] == _RUN_ID
        assert call_kwargs["diff_id"] == diff.diff_id
        assert call_kwargs["steps_count"] == 1
        assert call_kwargs["diff_operations"] == ["update_node"]


# ---------------------------------------------------------------------------
# Tests — GET /api/monitoring/cache  (SPEC-08 S-08.3)
# ---------------------------------------------------------------------------

# Build a minimal FastAPI app with the monitoring router for endpoint tests.

_monitoring_app = FastAPI()


def _override_cache_client() -> MagicMock:
    """Return a mock CacheClient for monitoring cache endpoint tests."""
    mock_cache = AsyncMock()
    mock_cache.info = AsyncMock(
        return_value={
            "used_memory": 1048576,
            "used_memory_human": "1.00M",
        }
    )
    mock_cache.dbsize = AsyncMock(return_value=42)
    return mock_cache


class TestCacheStatsEndpoint:
    """Verify GET /api/monitoring/cache returns valid CacheStatsResponse."""

    def setup_method(self) -> None:
        """Reset metrics before each test to get deterministic counters."""
        get_metrics().reset()

    def test_cache_stats_endpoint(self) -> None:
        """Endpoint returns CacheStatsResponse with expected fields from mock Redis."""
        from api.cache.client import get_cache_client
        from api.routers import monitoring_agents as monitoring_router

        app = FastAPI()
        app.include_router(monitoring_router.router, prefix="/api/monitoring")

        mock_cache = _override_cache_client()
        app.dependency_overrides[get_cache_client] = lambda: mock_cache

        # Seed hit/miss counters in MetricsCollector
        metrics = get_metrics()
        metrics.increment("cache_hits", value=80)
        metrics.increment("cache_misses", value=20)

        with patch.object(monitoring_router, "get_cache_client", return_value=mock_cache):
            with TestClient(app) as client:
                resp = client.get("/api/monitoring/cache")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["total_keys"] == 42
        assert data["memory_used_bytes"] == 1048576
        assert data["memory_used_human"] == "1.00M"
        assert data["hit_count"] == 80
        assert data["miss_count"] == 20
        assert "hit_ratio" in data

    def test_cache_stats_redis_unavailable(self) -> None:
        """Endpoint returns zeros when Redis is unreachable (fail-open)."""
        from api.routers import monitoring_agents as monitoring_router

        app = FastAPI()
        app.include_router(monitoring_router.router, prefix="/api/monitoring")

        mock_cache = AsyncMock()
        mock_cache.info = AsyncMock(return_value=None)
        mock_cache.dbsize = AsyncMock(return_value=0)

        with patch.object(monitoring_router, "get_cache_client", return_value=mock_cache):
            with TestClient(app) as client:
                resp = client.get("/api/monitoring/cache")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["total_keys"] == 0
        assert data["memory_used_bytes"] == 0
        assert data["hit_ratio"] == 0.0


class TestCacheStatsHitRatio:
    """Verify cache hit ratio calculation from hit/miss counters."""

    def setup_method(self) -> None:
        """Reset metrics before each test."""
        get_metrics().reset()

    def test_cache_stats_hit_ratio(self) -> None:
        """Hit ratio = hits / (hits + misses), rounded to 4 decimal places."""
        from api.routers import monitoring_agents as monitoring_router

        app = FastAPI()
        app.include_router(monitoring_router.router, prefix="/api/monitoring")

        mock_cache = AsyncMock()
        mock_cache.info = AsyncMock(return_value={"used_memory": 0, "used_memory_human": "0B"})
        mock_cache.dbsize = AsyncMock(return_value=0)

        metrics = get_metrics()
        metrics.increment("cache_hits", value=75)
        metrics.increment("cache_misses", value=25)

        with patch.object(monitoring_router, "get_cache_client", return_value=mock_cache):
            with TestClient(app) as client:
                resp = client.get("/api/monitoring/cache")

        data = resp.json()
        # 75 / (75 + 25) = 0.75
        assert data["hit_ratio"] == 0.75
        assert data["hit_count"] == 75
        assert data["miss_count"] == 25

    def test_cache_stats_hit_ratio_zero_total(self) -> None:
        """Hit ratio must be 0.0 when no cache operations have occurred."""
        from api.routers import monitoring_agents as monitoring_router

        app = FastAPI()
        app.include_router(monitoring_router.router, prefix="/api/monitoring")

        mock_cache = AsyncMock()
        mock_cache.info = AsyncMock(return_value={"used_memory": 0, "used_memory_human": "0B"})
        mock_cache.dbsize = AsyncMock(return_value=0)

        with patch.object(monitoring_router, "get_cache_client", return_value=mock_cache):
            with TestClient(app) as client:
                resp = client.get("/api/monitoring/cache")

        data = resp.json()
        assert data["hit_ratio"] == 0.0
        assert data["hit_count"] == 0
        assert data["miss_count"] == 0

    def test_cache_stats_hit_ratio_all_hits(self) -> None:
        """Hit ratio must be 1.0 when all cache operations are hits."""
        from api.routers import monitoring_agents as monitoring_router

        app = FastAPI()
        app.include_router(monitoring_router.router, prefix="/api/monitoring")

        mock_cache = AsyncMock()
        mock_cache.info = AsyncMock(return_value={"used_memory": 512, "used_memory_human": "512B"})
        mock_cache.dbsize = AsyncMock(return_value=10)

        metrics = get_metrics()
        metrics.increment("cache_hits", value=100)
        # No misses

        with patch.object(monitoring_router, "get_cache_client", return_value=mock_cache):
            with TestClient(app) as client:
                resp = client.get("/api/monitoring/cache")

        data = resp.json()
        assert data["hit_ratio"] == 1.0
        assert data["hit_count"] == 100
        assert data["miss_count"] == 0
