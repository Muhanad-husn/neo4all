"""
api/routers/health.py — GET /api/health

Per-service connectivity status. Probe logic is delegated entirely to
api.services.health (SKILL-B — no business logic in route handlers).

Response:
    HealthResponse  — per-service status (healthy/unhealthy), overall
                      status "success" (all healthy) or "partial".
"""

from fastapi import APIRouter

from api.config import get_settings
from api.observability.logger import get_logger
from api.routers.models import HealthResponse, ServiceStatus
from api.services.health import probe_all_services

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

_VERSION = "0.1.0"


@router.get("/health", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    """Return per-service connectivity status.

    Concurrently probes Neo4j, Redis, S3, and Qdrant. Returns overall
    status "success" when all services are reachable, "partial" when one
    or more are degraded.
    """
    settings = get_settings()
    results = await probe_all_services(settings)

    services = [
        ServiceStatus(name=r.name, healthy=r.healthy, error=r.error)
        for r in results
    ]
    all_healthy = all(s.healthy for s in services)
    overall = "success" if all_healthy else "partial"

    logger.info("health_checked", all_healthy=all_healthy, service_count=len(services))

    return HealthResponse(
        run_id="",
        status=overall,
        services=services,
        version=_VERSION,
    )
