"""
api/routers/config.py — Configuration reload endpoint.

POST /reload  — Clear the cached Settings singleton, reset the Neo4j client,
                re-probe all backend services, and return the results.

This endpoint is called by the Phase 0 UI form after writing updated
credentials to ``.env``. It allows the running API to pick up new credentials
without a process restart.

Security note: This endpoint is intended for local-development use where the
UI and API share a filesystem. In production, credentials come from
environment variables injected by the orchestrator and do not change at
runtime.
"""

from fastapi import APIRouter

from api.config import reload_settings
from api.graph.client import reset_neo4j_client
from api.observability.logger import get_logger
from api.routers.models import ConfigReloadResponse, ServiceStatus
from api.services.health import probe_all_services

logger = get_logger(__name__)

router = APIRouter()


@router.post("/reload", response_model=ConfigReloadResponse)
async def reload_config() -> ConfigReloadResponse:
    """Reload application configuration from ``.env`` and re-probe services.

    Steps:
        1. Clear the ``get_settings()`` LRU cache and re-read ``.env``.
        2. Close and discard the cached Neo4j client (it holds stale creds).
        3. Probe all backend services with the fresh settings.
        4. Return probe results so the caller can verify connectivity.

    Returns:
        ConfigReloadResponse with per-service status.
    """
    settings = reload_settings()

    await reset_neo4j_client()

    results = await probe_all_services(settings)

    services = [
        ServiceStatus(name=r.name, healthy=r.healthy, error=r.error)
        for r in results
    ]

    logger.info(
        "config_reloaded",
        services_healthy=sum(1 for s in services if s.healthy),
        services_total=len(services),
    )

    return ConfigReloadResponse(
        run_id="",
        status="ok",
        errors=[],
        services=services,
    )
