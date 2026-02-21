"""
api/config.py — Application configuration via Pydantic Settings.

All environment variables are loaded from the process environment (or .env file
for local development). The app fails closed on startup if any required variable
is absent — see CLAUDE.md Section 4.4.

Usage:
    from api.config import get_settings
    settings = get_settings()
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, validated configuration loaded from environment variables.

    Fields with no default are *required*. Pydantic raises ValidationError at
    instantiation time if any required variable is missing, which prevents the
    application from starting (fail-closed behaviour per CLAUDE.md §4.4).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Ignore unknown env vars so the container environment does not cause
        # spurious validation errors from unrelated variables.
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # Neo4j Aura — dev instance (required)
    # -------------------------------------------------------------------------
    NEO4J_DEV_URI: str
    NEO4J_DEV_USER: str
    NEO4J_DEV_PASSWORD: str

    # -------------------------------------------------------------------------
    # Neo4j Aura — CI instance (optional; integration tests skip when absent)
    # -------------------------------------------------------------------------
    NEO4J_CI_URI: str | None = None
    NEO4J_CI_USER: str | None = None
    NEO4J_CI_PASSWORD: str | None = None

    # -------------------------------------------------------------------------
    # LLM gateway
    # -------------------------------------------------------------------------
    OPENROUTER_API_KEY: str

    # -------------------------------------------------------------------------
    # Object storage — RustFS (local) / S3 (prod)
    # -------------------------------------------------------------------------
    S3_ENDPOINT_URL: str
    S3_ACCESS_KEY_ID: str
    S3_SECRET_ACCESS_KEY: str
    S3_BUCKET_NAME: str

    # -------------------------------------------------------------------------
    # Redis — shared by ARQ worker and cache layer
    # -------------------------------------------------------------------------
    REDIS_URL: str

    # -------------------------------------------------------------------------
    # Qdrant — optional for remote instance; omit to use in-process / local
    # -------------------------------------------------------------------------
    QDRANT_URL: str | None = None

    # -------------------------------------------------------------------------
    # Observability (SKILL-D)
    # -------------------------------------------------------------------------
    LOG_FORMAT: Literal["json", "console"] = "json"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the application settings singleton.

    The first call instantiates and validates Settings. Missing required
    environment variables cause a ValidationError, which the startup hook in
    api/main.py surfaces as a CRITICAL log entry and process exit.

    The result is cached so subsequent calls pay no I/O cost.
    """
    return Settings()
