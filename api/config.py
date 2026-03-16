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

from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Per-agent configuration (SPEC-07 S-07.8)
# ---------------------------------------------------------------------------


class AgentModelConfig(BaseModel):
    """LLM model assignment and token budgets for a single agent.

    Attributes:
        model:              OpenRouter model identifier.
        max_input_tokens:   Hard cap on prompt tokens sent to the model.
        max_output_tokens:  Hard cap on completion tokens requested.
    """

    model: str
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)


class AgentConfig(BaseModel):
    """Per-agent pipeline configuration (SPEC-07 S-07.8).

    Centralises model assignments, token budgets, cost limits, retrieval
    parameters, and reranking settings for the AI curation agents.
    Values are defaults — override via environment or Settings fields.

    Attributes:
        agent_a:               Model and token budgets for Agent-A (Evidence Assembly).
        agent_b:               Model and token budgets for Agent-B (Retrieval Augmentation).
        agent_p:               Model and token budgets for Agent-P (Proposal Composer).
        cost_limit_per_candidate: Maximum USD cost for the full agent chain
                                  processing a single candidate.
        max_retrieval_rounds:  Agent-B loop guard — maximum retrieval rounds
                               before auto-deferring the candidate.
        reranking_top_n:       Number of top chunks returned by reranking
                               before feeding to Agent-A/Agent-P.
    """

    agent_a: AgentModelConfig = AgentModelConfig(
        model="openrouter/hunter-alpha",
        max_input_tokens=8000,
        max_output_tokens=2000,
    )
    agent_b: AgentModelConfig = AgentModelConfig(
        model="openrouter/hunter-alpha",
        max_input_tokens=8000,
        max_output_tokens=1000,
    )
    agent_p: AgentModelConfig = AgentModelConfig(
        model="openrouter/hunter-alpha",
        max_input_tokens=8000,
        max_output_tokens=2000,
    )
    cost_limit_per_candidate: float = Field(default=0.10, gt=0.0)
    max_retrieval_rounds: int = Field(default=3, ge=1, le=10)
    reranking_top_n: int = Field(default=5, ge=1, le=50)


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
    # Accepts either the project's canonical names (NEO4J_DEV_*) or the names
    # that Neo4j Aura auto-generates in its downloaded .env files (NEO4J_URI /
    # NEO4J_USERNAME / NEO4J_PASSWORD) so both work without manual renaming.
    # -------------------------------------------------------------------------
    NEO4J_DEV_URI: str = Field(
        validation_alias=AliasChoices("NEO4J_DEV_URI", "NEO4J_URI")
    )
    NEO4J_DEV_USER: str = Field(
        validation_alias=AliasChoices("NEO4J_DEV_USER", "NEO4J_USERNAME")
    )
    NEO4J_DEV_PASSWORD: str = Field(
        validation_alias=AliasChoices("NEO4J_DEV_PASSWORD", "NEO4J_PASSWORD")
    )

    # -------------------------------------------------------------------------
    # Neo4j connection pool (SPEC-04 S-04.4)
    # -------------------------------------------------------------------------
    NEO4J_MAX_POOL_SIZE: int = 50
    NEO4J_CONNECTION_TIMEOUT_S: float = 30.0

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

    @field_validator("OPENROUTER_API_KEY")
    @classmethod
    def _openrouter_key_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("OPENROUTER_API_KEY must not be empty")
        return v

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

    # -------------------------------------------------------------------------
    # Ingestion parser toggles (SPEC-03 S-03.1)
    # Set to False to disable a tier; useful for environments where a parser
    # library is unavailable or for integration testing specific tiers.
    # All three default to True (full fallback chain enabled).
    # -------------------------------------------------------------------------
    ENABLE_DOCLING: bool = True
    ENABLE_UNSTRUCTURED: bool = True
    ENABLE_RAW_FALLBACK: bool = True

    # -------------------------------------------------------------------------
    # Agent-B feature gate (SPEC-07)
    # When True and Agent-A returns insufficient evidence, the pipeline routes
    # to Agent-B (retrieval augmentation) before proposal composition.
    # Default False for safe rollout — operators opt in when ready.
    # -------------------------------------------------------------------------
    ENABLE_AGENT_B: bool = False

    # -------------------------------------------------------------------------
    # Vector indexing — sentence-transformers embedding model (SPEC-03 S-03.5)
    # Must match the model used at query time so vectors are comparable.
    # Changing this after indexing requires re-ingestion of all documents.
    # -------------------------------------------------------------------------
    EMBEDDING_MODEL: str = "sentence-transformers/all-MiniLM-L6-v2"

    # -------------------------------------------------------------------------
    # Agent pipeline (SPEC-07 S-07.8)
    # Per-agent model assignment, token budgets, cost limits, retrieval
    # parameters.  Nested Pydantic model — overridable via env prefixed
    # fields or by constructing AgentConfig directly in tests.
    # -------------------------------------------------------------------------
    AGENT_CONFIG: AgentConfig = Field(default_factory=AgentConfig)

    # -------------------------------------------------------------------------
    # Dry-run mode (SPEC-08 S-08.1)
    # When True, Agent-C skips all graph mutations (Neo4j writes).  Diffs and
    # proposals are still generated and logged to S3 / structlog so operators
    # can review what *would* happen without touching the graph.
    # -------------------------------------------------------------------------
    DRY_RUN: bool = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the application settings singleton.

    The first call instantiates and validates Settings. Missing required
    environment variables cause a ValidationError, which the startup hook in
    api/main.py surfaces as a CRITICAL log entry and process exit.

    The result is cached so subsequent calls pay no I/O cost.
    """
    return Settings()


def reload_settings() -> Settings:
    """Clear the cached Settings singleton and re-read from environment/.env.

    Used by the config reload endpoint to pick up credential changes written
    to ``.env`` by the Phase 0 UI form without restarting the process.

    Returns:
        The freshly-loaded Settings instance.
    """
    get_settings.cache_clear()
    return get_settings()
