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
    # Per-call timeout for a one-shot (structured) request, and the max gap between successive
    # tokens in a stream before it's treated as stalled — not the total stream duration, since a
    # legitimate answer plus qwen3's thinking preamble can run well past either number in total.
    # See docs/ASSUMPTIONS_AND_TRADEOFFS.md trade-off 13 for the throughput this budget assumes.
    llm_request_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_stream_stall_timeout_seconds: float = Field(default=30.0, gt=0)
    # Fallback-chain circuit breaker (llm/chain.py): consecutive failures before a provider is
    # skipped, and how long it stays skipped before one trial request is allowed through again.
    llm_circuit_breaker_failure_threshold: int = Field(default=3, gt=0)
    llm_circuit_breaker_cooldown_seconds: float = Field(default=30.0, gt=0)

    # --- Agent graph (Cycle 3) ---
    # Bounded Validator -> Response retry loop (docs/ARCHITECTURE.md) — caps total LLM calls per
    # turn so a persistently-failing validation can't loop the graph indefinitely.
    max_validator_retries: int = Field(default=1, ge=0)
    # Rolling-summary memory (memory/summarizer.py): once a thread's raw message count exceeds
    # this, the oldest `memory_summarize_batch_size` messages are folded into the rolling summary
    # and dropped from state — bounding context growth within a long-running session.
    memory_max_verbatim_messages: int = Field(default=12, gt=0)
    memory_summarize_batch_size: int = Field(default=6, gt=0)

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

    @property
    def psycopg_dsn(self) -> str:
        """The same connection as `database_url`, but as a plain libpq conninfo string —
        `psycopg.AsyncConnection.connect()` and `AsyncPostgresSaver` speak this directly, not
        SQLAlchemy's `dialect+driver://` syntax. Used by the health check and, from Cycle 3, by
        `main.py`'s checkpointer pool — the one connection string, computed once, so the two can
        never drift apart the way a hand-written `.replace("+psycopg", "")` at each call site
        risked."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    # --- Auth (Cycle 2) ---
    jwt_secret_key: SecretStr | None = None
    jwt_expire_minutes: int = Field(default=60, gt=0)

    # --- Reranking budget guard (Cycle 1) ---
    rerank_enabled: bool = False
    rerank_monthly_budget: int = Field(default=450, gt=0)

    # --- Rate limiting (Cycle 2) ---
    rate_limit_capacity: int = Field(default=20, gt=0)
    rate_limit_refill_per_sec: float = Field(default=0.5, gt=0)

    # --- MCP server + client (Cycle 4) ---
    # The MCP server is a second local process (`python -m mcp_server`, docs/SETUP.md step 4)
    # speaking Streamable HTTP — the transport `mcp.server.mcpserver.MCPServer.run()` actually
    # supports in the installed SDK (see docs/ASSUMPTIONS_AND_TRADEOFFS.md assumption 7 on why
    # this is `MCPServer`, not the `FastMCP` name `docs/ARCHITECTURE.md`'s shorthand suggests).
    # Read by both sides so the client's URL and the server's bind address can never drift
    # apart, the same pattern `docker-compose.yml` uses for Postgres. Port 8100, not 8000,
    # because the FastAPI backend already owns 8000.
    mcp_server_host: str = "127.0.0.1"
    mcp_server_port: int = Field(default=8100, gt=0, le=65535)
    mcp_server_path: str = "/mcp"
    # Connecting to a down MCP server and calling one of its tools are different failure modes
    # (docs/ARCHITECTURE.md: "MCP server down -> tool marked unavailable, supervisor routes
    # around it") and so get separate budgets — a slow individual tool call should not be
    # mistaken for the server being unreachable at all, or vice versa.
    mcp_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    mcp_call_timeout_seconds: float = Field(default=10.0, gt=0)

    @property
    def mcp_server_url(self) -> str:
        """The single URL both `mcp_server/__main__.py` (what it binds) and
        `tools/mcp_client.py` (what it connects to) derive from these same three settings."""
        return f"http://{self.mcp_server_host}:{self.mcp_server_port}{self.mcp_server_path}"

    # --- Tools (Cycle 4) ---
    # Wall-clock budget for one `python_analysis` sandbox execution (`rlm/sandbox.py`). Enforced
    # with `asyncio.wait_for` around a thread, not a subprocess — this project's Windows event
    # loop is pinned to `SelectorEventLoop` (docs/ASSUMPTIONS_AND_TRADEOFFS.md trade-off 8),
    # which cannot spawn subprocesses, so subprocess-level sandbox isolation is not an option
    # here; see `rlm/sandbox.py`'s module docstring for what that does and does not protect against.
    sandbox_timeout_seconds: float = Field(default=5.0, gt=0)

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
