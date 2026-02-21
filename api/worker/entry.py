"""
api/worker/entry.py — ARQ worker entry point (SPEC-01).

Worker jobs are not yet implemented (SPEC-04 through SPEC-07).
This module provides the `arq-worker` console script target declared
in pyproject.toml. It will be populated in later increments.
"""

from api.config import get_settings
from api.observability.logger import configure_logging, get_logger

logger = get_logger(__name__)


def run() -> None:
    """Start the ARQ worker process.

    Validates required environment variables (fail-closed) then
    delegates to the ARQ worker loop. Job definitions will be
    registered in SPEC-04 through SPEC-07.
    """
    settings = get_settings()
    configure_logging(log_format=settings.LOG_FORMAT, log_level=settings.LOG_LEVEL)
    logger.info("arq_worker_starting", redis_url=settings.REDIS_URL)

    # ARQ worker loop — job classes registered in later increments.
    # arq.run_worker(WorkerSettings) will replace this placeholder.
    logger.warning(
        "arq_worker_no_jobs_registered",
        note="Worker job classes are implemented in SPEC-04 through SPEC-07.",
    )
