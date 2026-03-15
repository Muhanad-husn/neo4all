"""
api/main.py — FastAPI application factory and entry point.

Architecture
------------
  create_app()  Builds the FastAPI instance: registers middleware and routers.
                No business logic lives here (SKILL-B).
  lifespan()    Async context manager: startup validation → serve → shutdown.
  app           Module-level instance for: uvicorn api.main:app
  run()         pyproject.toml entry point:  api-server = "api.main:run"

Startup sequence
----------------
  1. Load and validate Settings — fail-closed on missing env vars (CLAUDE.md §4.4).
  2. Configure centralized structlog logging (SKILL-D R-D1).
  3. Concurrently probe Neo4j, Redis, S3, and Qdrant; log each result.
     Unreachable services are logged at WARNING — the app continues and the
     health endpoint surfaces degraded status to callers.

Middleware registration order
-----------------------------
  FastAPI processes middleware in reverse-registration order (last added =
  outermost). We register CORSMiddleware first so that CORS headers are
  applied after CorrelationMiddleware has injected the correlation ID.
"""

import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.observability.logger import configure_logging, get_logger
from api.observability.middleware import CorrelationMiddleware

logger = get_logger(__name__)

_VERSION = "1.0.4"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup probes and graceful shutdown.

    Startup:
        - Configure structured logging from Settings.
        - Probe all backend services concurrently.
        - Log readiness or degradation per service.
    Shutdown:
        - Emit shutdown log events.
    """
    from api.config import get_settings
    from api.credentials import refresh_credentials
    from api.services.health import probe_all_services

    settings = get_settings()
    configure_logging(log_format=settings.LOG_FORMAT, log_level=settings.LOG_LEVEL)

    logger.info("app_starting", version=_VERSION)

    # Pull credentials from Redis in case the user set them via the UI
    # before this container (re)started.  The credential override is
    # stored in-memory — no need to re-fetch Settings afterwards.
    await refresh_credentials()

    results = await probe_all_services(settings)
    for result in results:
        if result.healthy:
            logger.info(
                "service_ready",
                service=result.name,
                latency_ms=result.latency_ms,
            )
        else:
            logger.warning(
                "service_unreachable",
                service=result.name,
                error=result.error,
            )

    logger.info("app_started", version=_VERSION)

    yield

    logger.info("app_stopping")
    logger.info("app_stopped")


def create_app() -> FastAPI:
    """Construct the FastAPI application with middleware and routers.

    Imports routers inside the function body to keep the module-level
    import graph clean and avoid circular imports.

    Returns:
        Configured FastAPI instance ready to serve requests.
    """
    from api.routers import agent_pipeline as agent_pipeline_router
    from api.routers import approvals as approvals_router
    from api.routers import candidates as candidates_router
    from api.routers import config as config_router
    from api.routers import curation as curation_router
    from api.routers import documents as documents_router
    from api.routers import evidence as evidence_router
    from api.routers import extraction as extraction_router
    from api.routers import graph_explorer as graph_explorer_router
    from api.routers import health as health_router
    from api.routers import monitoring as monitoring_router
    from api.routers import monitoring_agents as monitoring_agents_router
    from api.routers import schema as schema_router
    from api.routers import session as session_router

    app = FastAPI(
        title="neo4all API",
        description="AI-Powered Graph Extraction & Curation Platform",
        version=_VERSION,
        lifespan=lifespan,
    )

    # CORS — allow all origins for local development.
    # allow_credentials must be False when allow_origins=["*"] (RFC 6454).
    # Restrict allow_origins in production deployments.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Correlation ID injection and request lifecycle logging (SKILL-D R-D2).
    # Registered after CORS so it wraps the route handlers directly.
    app.add_middleware(CorrelationMiddleware)

    # --- Routers ---
    # /api/health
    app.include_router(health_router.router, prefix="/api")
    # /api/monitoring/health  /api/monitoring/logs/recent  /api/monitoring/run/{id}
    # /api/monitoring/workers  /api/monitoring/jobs/{run_id}
    app.include_router(monitoring_router.router, prefix="/api/monitoring")
    # /api/monitoring/metrics  /api/monitoring/agents/{run_id}  /api/monitoring/cache
    app.include_router(monitoring_agents_router.router, prefix="/api/monitoring")
    # /api/schema/propose  /api/schema/approve  /api/schema/{run_id}
    app.include_router(schema_router.router, prefix="/api/schema")
    # /api/documents/ingest  /api/documents/{run_id}  /api/documents/{run_id}/{doc_id}/chunks
    app.include_router(documents_router.router, prefix="/api/documents")
    # /api/extraction/run  /api/extraction/status/{run_id}  /api/extraction/results/{run_id}
    app.include_router(extraction_router.router, prefix="/api/extraction")
    # /api/curation/candidates/generate  /api/curation/candidates/{run_id}
    app.include_router(candidates_router.router, prefix="/api/curation")
    # /api/curation/propose  /api/curation/proposals/{run_id}
    # /api/curation/proposals/{id}/execute
    app.include_router(curation_router.router, prefix="/api/curation")
    # /api/curation/evidence/{candidate_id}  /api/curation/evidence/query
    app.include_router(evidence_router.router, prefix="/api/curation")
    # /api/curation/proposals/{id}/approve  /reject  /defer  /confirm
    app.include_router(approvals_router.router, prefix="/api/curation")
    # /api/curation/agents/config  /api/curation/agents/run
    # /api/curation/agents/status/{run_id}
    app.include_router(agent_pipeline_router.router, prefix="/api/curation")
    # /api/graph/nodes/{run_id}  /api/graph/edges/{run_id}  (+ /count variants)
    app.include_router(graph_explorer_router.router, prefix="/api/graph")
    # /api/config/reload
    app.include_router(config_router.router, prefix="/api/config")
    # /api/session/save  /api/session/{user_hash}
    app.include_router(session_router.router, prefix="/api/session")

    return app


# Module-level instance consumed by: uvicorn api.main:app
app: FastAPI = create_app()


def run() -> None:
    """Start the uvicorn server.

    Entry point declared in pyproject.toml:
        api-server = "api.main:run"

    Settings are validated before uvicorn starts so that a missing env var
    produces a CRITICAL message and a clean exit rather than a raw Pydantic
    traceback from inside uvicorn's startup machinery.
    """
    import uvicorn

    from api.config import get_settings

    try:
        settings = get_settings()
    except Exception as exc:
        sys.stderr.write(f"CRITICAL: configuration error — cannot start: {exc}\n")
        sys.exit(1)

    configure_logging(log_format=settings.LOG_FORMAT, log_level=settings.LOG_LEVEL)

    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
