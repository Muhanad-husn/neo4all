"""
api/routers/config.py — Configuration reload endpoint.

POST /reload  — Publish credentials to Redis (single source of truth),
                reset the Neo4j client, re-probe all backend services,
                and return the results.

This endpoint is called by the Phase 0 UI form after the user enters
credentials.  Credentials are published to Redis via
``api.credentials.publish_credentials()`` so that both the API and
worker containers read from the same authoritative store.

Security note: In production, credentials come from environment variables
injected by the orchestrator and do not change at runtime.
"""

from fastapi import APIRouter

from api.config import get_settings
from api.credentials import publish_credentials
from api.graph.client import reset_neo4j_client
from api.observability.logger import get_logger
from api.routers.models import (
    ConfigReloadRequest,
    ConfigReloadResponse,
    RuntimeCredentials,
    ServiceStatus,
)
from api.services.health import probe_all_services

logger = get_logger(__name__)

router = APIRouter()


@router.post("/reload", response_model=ConfigReloadResponse)
async def reload_config(
    body: ConfigReloadRequest | None = None,
) -> ConfigReloadResponse:
    """Reload application configuration and re-probe services.

    Steps:
        1. If credentials are provided, publish to Redis and refresh
           the in-memory credential override.
        2. Reset the cached Neo4j client (it may hold stale creds).
        3. Probe all backend services with fresh credentials.
        4. Return probe results so the caller can verify connectivity.

    Returns:
        ConfigReloadResponse with per-service status.
    """
    if body is not None:
        creds = RuntimeCredentials(
            neo4j_uri=getattr(body, "neo4j_uri", None),
            neo4j_user=getattr(body, "neo4j_user", None),
            neo4j_password=getattr(body, "neo4j_password", None),
            openrouter_api_key=getattr(body, "openrouter_api_key", None),
        )
        await publish_credentials(creds)

    await reset_neo4j_client()

    settings = get_settings()
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
        status="success",
        errors=[],
        services=services,
    )
