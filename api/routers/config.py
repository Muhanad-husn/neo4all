"""
api/routers/config.py — Configuration reload endpoint.

POST /reload  — Optionally inject credentials into the process environment,
                clear the cached Settings singleton, reset the Neo4j client,
                re-probe all backend services, and return the results.

This endpoint is called by the Phase 0 UI form after the user enters
credentials.  In Docker the UI and API have separate filesystems, so the
UI sends credentials in the request body and this endpoint injects them
into ``os.environ`` before reloading settings.  When no body is provided
the endpoint falls back to re-reading ``.env`` (local-dev workflow).

Security note: In production, credentials come from environment variables
injected by the orchestrator and do not change at runtime.
"""

import os

from fastapi import APIRouter

from api.cache.client import get_cache_client
from api.cache.keys import CacheKey
from api.config import reload_settings
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

# Mapping from request field names to the env-var names that Pydantic
# Settings reads (using the primary alias accepted by AliasChoices).
_FIELD_TO_ENVS: dict[str, list[str]] = {
    "neo4j_uri": ["NEO4J_URI", "NEO4J_DEV_URI"],
    "neo4j_user": ["NEO4J_USERNAME", "NEO4J_DEV_USER"],
    "neo4j_password": ["NEO4J_PASSWORD", "NEO4J_DEV_PASSWORD"],
    "openrouter_api_key": ["OPENROUTER_API_KEY"],
}


@router.post("/reload", response_model=ConfigReloadResponse)
async def reload_config(
    body: ConfigReloadRequest | None = None,
) -> ConfigReloadResponse:
    """Reload application configuration and re-probe services.

    Steps:
        1. If credentials are provided in the request body, inject them
           into ``os.environ`` so the next ``Settings()`` picks them up.
        2. Clear the ``get_settings()`` LRU cache and re-read settings.
        3. Close and discard the cached Neo4j client (it holds stale creds).
        4. Probe all backend services with the fresh settings.
        5. Return probe results so the caller can verify connectivity.

    Returns:
        ConfigReloadResponse with per-service status.
    """
    # --- Inject credentials from request body into process env -----------
    if body is not None:
        injected = []
        for field_name, env_names in _FIELD_TO_ENVS.items():
            value = getattr(body, field_name, None)
            if value is not None and value.strip():
                for env_name in env_names:
                    os.environ[env_name] = value.strip()
                injected.append(env_names[0])
        if injected:
            logger.info(
                "credentials_injected_from_request",
                keys=injected,
            )

        # --- Publish credentials to Redis for worker container pickup ------
        creds = RuntimeCredentials(
            neo4j_uri=getattr(body, "neo4j_uri", None),
            neo4j_user=getattr(body, "neo4j_user", None),
            neo4j_password=getattr(body, "neo4j_password", None),
            openrouter_api_key=getattr(body, "openrouter_api_key", None),
        )
        cache = get_cache_client()
        stored = await cache.set(CacheKey.config_credentials(), creds)
        if stored:
            logger.info("credentials_published_to_redis", keys=injected)
        else:
            logger.warning("credentials_publish_failed")

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
        status="success",
        errors=[],
        services=services,
    )
