"""
api/services/health.py — Async service connectivity probes (SKILL-B).

Business logic for health and monitoring endpoints lives here so that route
handlers remain thin (SKILL-B — no business logic in routers). Each probe
is an independent async function that returns a typed ProbeResult without
raising.

Graceful failure
----------------
Every probe catches all exceptions, logs at WARNING, and returns
healthy=False with an error description. This is consistent with the
graceful degradation policy in SKILL-D R-D9.

Sensitive data
--------------
Credentials (passwords, keys, connection strings) are never logged
(SKILL-D R-D5). Error messages from drivers may contain endpoint hostnames
but not secrets — those strings are logged as-is.

ProbeResult uses Pydantic BaseModel so that the object crosses the
service → router module boundary as a typed contract (SKILL-A R-A3).
"""

import asyncio
import time

from pydantic import BaseModel

from api.config import Settings
from api.observability.logger import get_logger

logger = get_logger(__name__)


class ProbeResult(BaseModel):
    """Typed result of a single service connectivity probe (SKILL-A R-A3)."""

    name: str
    healthy: bool
    latency_ms: float | None = None
    error: str | None = None


async def probe_neo4j(uri: str, user: str, password: str) -> ProbeResult:
    """Probe Neo4j Aura connectivity via driver.verify_connectivity().

    Creates a short-lived async driver, verifies the handshake, then closes.
    The URI and credentials are never logged (SKILL-D R-D5).
    """
    t0 = time.perf_counter()
    try:
        from neo4j import AsyncGraphDatabase  # type: ignore[import-untyped]

        driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        await driver.verify_connectivity()
        await driver.close()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.debug("probe_neo4j_ok", latency_ms=latency_ms)
        return ProbeResult(name="neo4j", healthy=True, latency_ms=latency_ms)
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.warning("probe_neo4j_failed", latency_ms=latency_ms, error=str(exc))
        return ProbeResult(name="neo4j", healthy=False, latency_ms=latency_ms, error=str(exc))


async def probe_redis() -> ProbeResult:
    """Probe Redis via CacheClient.ping().

    Uses the process-level CacheClient singleton so that the latency
    measurement reflects a warm-connection round-trip after the first call.
    """
    from api.cache.client import get_cache_client

    t0 = time.perf_counter()
    cache = get_cache_client()
    healthy = await cache.ping()
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    if not healthy:
        logger.warning("probe_redis_failed", latency_ms=latency_ms)
        return ProbeResult(name="redis", healthy=False, latency_ms=latency_ms, error="ping failed")

    logger.debug("probe_redis_ok", latency_ms=latency_ms)
    return ProbeResult(name="redis", healthy=True, latency_ms=latency_ms)


async def probe_s3(
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
    bucket_name: str,
) -> ProbeResult:
    """Probe S3/RustFS by verifying bucket existence via HeadBucket.

    boto3 is synchronous; the call is offloaded to a thread via
    asyncio.to_thread to avoid blocking the event loop. Credentials are
    never logged (SKILL-D R-D5). The bucket name is safe to log.
    """

    def _ensure_bucket() -> None:
        import boto3  # type: ignore[import-untyped]
        from botocore.exceptions import ClientError

        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )
        try:
            client.head_bucket(Bucket=bucket_name)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchBucket"):
                logger.info("s3_bucket_creating", bucket=bucket_name)
                client.create_bucket(Bucket=bucket_name)
            else:
                raise

    t0 = time.perf_counter()
    try:
        await asyncio.to_thread(_ensure_bucket)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.debug("probe_s3_ok", latency_ms=latency_ms, bucket=bucket_name)
        return ProbeResult(name="s3", healthy=True, latency_ms=latency_ms)
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.warning("probe_s3_failed", latency_ms=latency_ms, bucket=bucket_name, error=str(exc))
        return ProbeResult(name="s3", healthy=False, latency_ms=latency_ms, error=str(exc))


async def probe_qdrant(qdrant_url: str | None) -> ProbeResult:
    """Probe Qdrant by listing collections.

    If qdrant_url is None, Qdrant is assumed to run in-process (no external
    probe needed) and the service is reported as healthy by convention.
    """
    if not qdrant_url:
        return ProbeResult(name="qdrant", healthy=True, latency_ms=None)

    t0 = time.perf_counter()
    try:
        from qdrant_client import AsyncQdrantClient

        client = AsyncQdrantClient(url=qdrant_url)
        try:
            await client.get_collections()
        finally:
            await client.close()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.debug("probe_qdrant_ok", latency_ms=latency_ms)
        return ProbeResult(name="qdrant", healthy=True, latency_ms=latency_ms)
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        logger.warning("probe_qdrant_failed", latency_ms=latency_ms, error=str(exc))
        return ProbeResult(name="qdrant", healthy=False, latency_ms=latency_ms, error=str(exc))


async def probe_all_services(settings: Settings) -> list[ProbeResult]:
    """Concurrently probe all configured backend services.

    All probes run in parallel via asyncio.gather. Result order is stable:
    [neo4j, redis, s3, qdrant].

    Args:
        settings: Application settings providing connection parameters.
                  Credentials are passed directly to probes and never logged.

    Returns:
        List of ProbeResult, one per service. Never raises.
    """
    from api.credentials import get_credentials
    creds = get_credentials()
    results = await asyncio.gather(
        probe_neo4j(
            creds.neo4j_uri,
            creds.neo4j_user,
            creds.neo4j_password,
        ),
        probe_redis(),
        probe_s3(
            settings.S3_ENDPOINT_URL,
            settings.S3_ACCESS_KEY_ID,
            settings.S3_SECRET_ACCESS_KEY,
            settings.S3_BUCKET_NAME,
        ),
        probe_qdrant(settings.QDRANT_URL),
    )
    return list(results)
