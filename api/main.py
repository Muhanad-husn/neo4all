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

_VERSION = "0.1.0"


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
    from api.services.health import probe_all_services

    settings = get_settings()
    configure_logging(log_format=settings.LOG_FORMAT, log_level=settings.LOG_LEVEL)

    logger.info("app_starting", version=_VERSION)

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
    from api.routers import health as health_router
    from api.routers import monitoring as monitoring_router

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
    app.include_router(monitoring_router.router, prefix="/api/monitoring")

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
