"""
api/worker/entry.py — ARQ worker entry point (SPEC-04 S-04.1).

Declares WorkerSettings for the ARQ worker process:
  - functions = [extraction_job] — the only job registered in SPEC-04.
  - on_startup / on_shutdown hooks: open and close the Neo4j driver once per
    worker process, storing the client in ctx["neo4j_client"] so jobs can
    construct a GraphWriter without re-opening the pool on every call.
  - max_jobs: maximum concurrent jobs per worker process (default 10).
  - job_timeout: seconds before ARQ forcibly kills a stalled job (default 300).
  - health_check_interval: seconds between ARQ heartbeat writes (default 30).

redis_settings is NOT defined at class level. run() resolves REDIS_URL from
Settings and injects it into WorkerSettings before calling arq.run_worker().
This prevents get_settings() from executing on module import, allowing test
environments to import WorkerSettings without all environment variables present.

Console script entry point (pyproject.toml):
    arq-worker = "api.worker.entry:run"

Sensitive data (SKILL-D R-D5):
    Only the host fragment of REDIS_URL is written to the log — never
    credentials or the full connection string.
"""

from __future__ import annotations

from api.graph.client import get_neo4j_client
from api.observability.logger import configure_logging, get_logger
from api.worker.jobs import (
    evidence_assembly_job,
    extraction_job,
    proposal_composition_job,
    retrieval_augmentation_job,
)

logger = get_logger(__name__)

_MAX_JOBS: int = 10
_JOB_TIMEOUT_S: int = 300          # 5 minutes — covers large-chunk LLM round-trips
_HEALTH_CHECK_INTERVAL_S: int = 30  # seconds between ARQ heartbeat Redis writes


# ---------------------------------------------------------------------------
# ARQ lifecycle hooks
# ---------------------------------------------------------------------------


async def startup(ctx: dict) -> None:
    """Open Neo4j driver once for all jobs on this worker process.

    Stores the client in ctx["neo4j_client"] so extraction_job can retrieve
    it without re-opening the connection pool per call. The driver is opened
    with the pooling and timeout settings from Settings (NEO4J_MAX_POOL_SIZE,
    NEO4J_CONNECTION_TIMEOUT_S).

    Log events:
        arq_worker_started  INFO — max_jobs
    """
    client = get_neo4j_client()
    client.open()
    ctx["neo4j_client"] = client
    logger.info("arq_worker_started", max_jobs=_MAX_JOBS)


async def shutdown(ctx: dict) -> None:
    """Close the Neo4j driver on worker process shutdown.

    Drains the connection pool gracefully. Safe to call if the driver was
    never opened (ctx key absent).

    Log events:
        arq_worker_stopped  INFO
    """
    client = ctx.get("neo4j_client")
    if client is not None:
        await client.close()
    logger.info("arq_worker_stopped")


# ---------------------------------------------------------------------------
# WorkerSettings — ARQ reads class attributes at run_worker() call time
# ---------------------------------------------------------------------------


class WorkerSettings:
    """ARQ configuration for the extraction pipeline.

    ARQ reads class attributes directly (not via __init__), so all settings
    are declared as class-level variables.

    redis_settings is absent here. run() injects it as a class attribute
    before calling arq.run_worker(WorkerSettings), so ARQ sees it at the
    point it needs it — after the process has started and env vars are
    available, but before the event loop begins.

    Attributes:
        functions:             Jobs registered with this worker. New jobs from
                               later increments (SPEC-05 through SPEC-07) must
                               be appended here.
        on_startup:            Async hook called once after connecting to Redis.
        on_shutdown:           Async hook called once before worker exit.
        max_jobs:              Maximum jobs executing concurrently on this worker.
        job_timeout:           Seconds ARQ waits before killing a stalled job.
        health_check_interval: Seconds between ARQ heartbeat writes to Redis.
        queue_name:            Redis list key that holds enqueued jobs.
    """

    functions = [
        extraction_job,
        evidence_assembly_job,
        retrieval_augmentation_job,
        proposal_composition_job,
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = _MAX_JOBS
    job_timeout = _JOB_TIMEOUT_S
    health_check_interval = _HEALTH_CHECK_INTERVAL_S
    queue_name = "arq:queue"
    # redis_settings: injected by run() before arq.run_worker(WorkerSettings)


# ---------------------------------------------------------------------------
# Console script entry point
# ---------------------------------------------------------------------------


def run() -> None:
    """Start the ARQ worker process.

    Resolves REDIS_URL from Settings (fail-closed: ValidationError if absent),
    configures structured logging, injects redis_settings into WorkerSettings,
    then delegates to arq.run_worker() which drives the asyncio event loop
    until the process receives a shutdown signal.

    Log events:
        arq_worker_starting  INFO — redis_host
    """
    import arq
    from arq.connections import RedisSettings

    from api.config import get_settings

    settings = get_settings()
    configure_logging(log_format=settings.LOG_FORMAT, log_level=settings.LOG_LEVEL)

    # Resolve Redis connection at startup — fail-closed on invalid REDIS_URL.
    WorkerSettings.redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)

    # Log only the host fragment — never credentials (SKILL-D R-D5).
    redis_url = settings.REDIS_URL
    safe_host = redis_url.split("@")[-1] if "@" in redis_url else redis_url
    logger.info("arq_worker_starting", redis_host=safe_host)

    arq.run_worker(WorkerSettings)
