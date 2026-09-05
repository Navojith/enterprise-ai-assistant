"""Application configuration.

A single `pydantic-settings` model reads and validates every environment variable the
project uses, so a missing or malformed value fails fast at startup with a clear error
instead of surfacing later as a confusing runtime failure deep in a graph node.

Secrets that genuinely have no safe default (Pinecone, LangSmith, the JWT signing key) are
`Optional`, not required, because Cycle 0 must boot without those accounts existing yet
(docs/PROGRESS.md tracks them as external prerequisites for Cycles 1 and 3). The modules that
actually need them are responsible for raising `ConfigurationError` at first use if they are
still `None` by the time that cycle's code runs — see docs/DECISIONS.md §8.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Pinecone Starter includes 500 rerank requests/month (docs/DECISIONS.md §4). The budget is
# kept strictly below that ceiling so the persisted counter can disable reranking *before* the
# free tier is exhausted, never at it.
_PINECONE_STARTER_RERANK_CEILING = 500


class Settings(BaseSettings):
    """Environment-backed configuration. See `.env.example` for the full variable list."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # A variable present in .env but left blank (`DB_HOST=`) means "use the default", not
        # "the value is the empty string" — without this, an unfilled secret or host would
        # resolve to SecretStr("") / "" rather than falling back to a working local default.
        env_ignore_empty=True,
    )

    # --- Application ---
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- Pinecone (Cycle 1) ---
    pinecone_api_key: SecretStr | None = None
    pinecone_dense_index: str = "enterprise-assistant-dense"
    pinecone_sparse_index: str = "enterprise-assistant-sparse"

    # --- LangSmith (Cycle 3) ---
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "enterprise-ai-assistant"
    langsmith_tracing: bool = False

    # --- Ollama (Cycle 3) ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:4b"

    # --- Postgres ---
    # Held as separate fields, not a single DSN, so docker-compose.yml can provision the
    # container from the same names (it reads this same .env file for variable substitution)
    # without the container's credentials and the app's connection string drifting apart.
    db_host: str = "localhost"
    db_port: int = Field(default=5433, gt=0, le=65535)
    db_name: str = "enterprise_ai_assistant"
    db_user: str = "postgres"
    db_password: SecretStr = SecretStr("postgres")

    @property
    def database_url(self) -> str:
        """Assemble the SQLAlchemy/psycopg async DSN from the granular DB_* settings.

        User and password are percent-encoded because a DSN embeds them positionally — an
        unescaped `:` or `@` in either would otherwise be parsed as a URL delimiter instead of
        as part of the credential.
        """
        user = quote(self.db_user, safe="")
        password = quote(self.db_password.get_secret_value(), safe="")
        return (
            f"postgresql+psycopg://{user}:{password}@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # --- Auth (Cycle 2) ---
    jwt_secret_key: SecretStr | None = None
    jwt_expire_minutes: int = Field(default=60, gt=0)

    # --- Reranking budget guard (Cycle 1) ---
    rerank_enabled: bool = False
    rerank_monthly_budget: int = Field(default=450, gt=0)

    # --- Rate limiting (Cycle 2) ---
    rate_limit_capacity: int = Field(default=20, gt=0)
    rate_limit_refill_per_sec: float = Field(default=0.5, gt=0)

    @field_validator("rerank_monthly_budget")
    @classmethod
    def _budget_below_free_tier_ceiling(cls, value: int) -> int:
        if value >= _PINECONE_STARTER_RERANK_CEILING:
            raise ValueError(
                f"RERANK_MONTHLY_BUDGET ({value}) must stay below the Pinecone Starter free "
                f"tier's {_PINECONE_STARTER_RERANK_CEILING} requests/month — see "
                "docs/DECISIONS.md §4. Raising it to or past the ceiling risks a billed request."
            )
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings factory for FastAPI dependency injection (`Depends(get_settings)`).

    `lru_cache` rather than a module-level `Settings()` instance so tests can override the
    environment and call `get_settings.cache_clear()` to force a fresh read.
    """
    return Settings()
