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
    # 90s, not 30s: live testing found `rlm/api.py`'s sub-agent/aggregate calls — which run with
    # `reasoning=True`, unlike the Supervisor's routing calls — routinely need well over 30s on
    # this hardware even for a short prompt (one measured run: 12.7s and 693 tokens of chain-of-
    # thought for a trivial 3-word request), and 3 consecutive timeouts at the old 30s tripped
    # the circuit breaker below, failing the *entire* turn including the unrelated Response node,
    # not just the slow research turn that caused it (docs/ASSUMPTIONS_AND_TRADEOFFS.md
    # trade-off 27).
    llm_request_timeout_seconds: float = Field(default=90.0, gt=0)
    llm_stream_stall_timeout_seconds: float = Field(default=30.0, gt=0)
    # Fallback-chain circuit breaker (llm/chain.py): consecutive failures before a provider is
    # skipped, and how long it stays skipped before one trial request is allowed through again.
    # Threshold raised 3 -> 5 alongside the timeout increase above, for the same reason: a
    # research turn's several sequential reasoning-heavy calls should have a little more room for
    # one or two of them running long before the whole turn is abandoned, without disabling the
    # breaker's real purpose (a genuinely unreachable Ollama still opens it well within a turn).
    llm_circuit_breaker_failure_threshold: int = Field(default=5, gt=0)
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

    # --- RLM research agent (Cycle 5) ---
    # A top-level research plan's own sandbox call bridges out to potentially several LLM calls
    # (plan generation, up to `rlm_max_total_sub_agent_calls` sub-agent analyses, aggregation —
    # see `rlm/api.py`'s sync/async bridge), so it needs a far larger wall-clock budget than
    # `sandbox_timeout_seconds`'s single-call one; kept as a separate setting rather than
    # widening that one, since `python_analysis` genuinely should stay fast. 180s, not 90s:
    # verified live that with `rlm_max_concurrent_sub_agents=1` (sequential, deliberately — see
    # that setting's own docstring) four sequential sub-agent calls plus plan generation,
    # search and aggregation on this hardware's `qwen3:4b` genuinely takes 90-150s end to end;
    # 90s cut a real, otherwise-successful run off mid-aggregation. This is the honest cost of
    # trading concurrency for reliability on a single local 4B model, not padding for its own
    # sake — `docs/DECISIONS.md` §3's "60-90s per question" budget assumed the RLM path would
    # look like the rest of the graph (a handful of fast schema-constrained calls), which does
    # not hold once a research turn genuinely fans out to multiple sequential sub-agent analyses.
    # Raised again, 180s -> 450s, alongside `llm_request_timeout_seconds` above
    # (docs/ASSUMPTIONS_AND_TRADEOFFS.md trade-off 27): once individual calls were given more
    # room to actually succeed instead of timing out at 30s, the same sequential chain's *total*
    # wall-clock cost grew with it — a real completed run was measured at ~350s end to end
    # (a validated plan-generation failure, a fallback plan's own search, and 4 real sequential
    # sub-agent analyses each running full reasoning), and needs headroom above that, not exactly
    # up to it.
    # Raised a third time, 450s -> 750s, alongside `rlm_max_total_sub_agent_calls` below
    # (docs/ASSUMPTIONS_AND_TRADEOFFS.md trade-off 28): once `rlm/api.py::group_by_document`
    # made batching correctly respect document boundaries, covering a broad question's real
    # evidence needs more sequential sub-agent calls than 4 comfortably fits inside 450s — live
    # verification measured individual reasoning-enabled sub-agent/aggregate calls at 55-90s each
    # on this hardware. 750s gives 8 sequential sub-agent calls plus plan generation, search, and
    # aggregation genuine headroom rather than cutting a real run off mid-way, the same reasoning
    # as the 180s -> 450s raise above, just for a larger budget.
    rlm_plan_timeout_seconds: float = Field(default=750.0, gt=0)
    # How many levels of *plan generation* `sub_agent` may recurse through before bottoming out
    # to one direct, non-recursive LLM analysis — see `rlm/api.py`'s module docstring. Defaults
    # to 1 (a top-level plan's `sub_agent` calls always bottom out to a single leaf analysis,
    # never a nested plan) rather than the structurally-supported 2+: verified live that depth 2
    # made a *single* top-level `sub_agents` call recurse into up to `rlm_max_concurrent_sub_agents`
    # concurrent *nested plan-generation* calls — each an uncounted LLM call on top of
    # `RLMBudget`'s own cap — which is exactly the "second resident model" style overload
    # `docs/DECISIONS.md` §3 already forbids, just self-inflicted by recursion width instead of
    # a second model. A deployment with real spare LLM throughput can raise this back to 2+.
    rlm_max_depth: int = Field(default=1, ge=1)
    # Semaphore size bounding how many `sub_agent` analyses `sub_agents` runs concurrently —
    # docs/ARCHITECTURE.md's "bounded semaphore that prevents the RLM from saturating a single
    # local model". Defaults to 1 (effectively sequential) rather than higher, for the same
    # hardware reason `docs/DECISIONS.md` §3 pins the whole graph to one resident model: verified
    # live that 4 concurrent `astructured` calls against one local `qwen3:4b` instance do not run
    # in parallel — Ollama serializes them, so three of the four sat queued long enough to exceed
    # `llm_request_timeout_seconds` and trip `llm/chain.py`'s circuit breaker, failing the whole
    # turn (including the unrelated Response node, since only one provider tier is configured).
    # Sequential sub-agent calls total the same latency a single local model would need anyway,
    # just without the self-inflicted timeouts; raise this only for an LLM backend with real
    # spare concurrent capacity (a cloud tier, or multiple resident models).
    rlm_max_concurrent_sub_agents: int = Field(default=1, ge=1)
    # A whole-recursive-tree ceiling on `sub_agent` calls, shared across every depth via
    # `rlm.api.RLMBudget` — bounds *width*, which `rlm_max_depth` alone does not, keeping a
    # research turn's total LLM calls inside docs/DECISIONS.md §3's per-question latency budget
    # regardless of how the generated plan's tree happens to be shaped.
    #
    # Raised 4 -> 8 (docs/ASSUMPTIONS_AND_TRADEOFFS.md trade-off 28): 4 was tuned against a
    # fixed-size `batch()` that (before that trade-off's fix) happened to spread thin across many
    # incidents inaccurately — one incident's sections split across different batches. Once
    # `rlm/api.py::group_by_document` made batching correctly respect document boundaries, 4
    # calls covers only a handful of whole documents, well under the seed corpus's real ~10-15
    # payment incidents for the spec's own example question — live-verified producing an honest
    # but thin "no recurring root cause identified" over a handful of documents instead of a
    # comprehensive answer. 8 comfortably covers that corpus's real incident count at
    # `group_by_document`'s default `max_batch_size=8` (roughly 2 incidents per batch), at the
    # cost of up to twice as many sequential local-model calls per research turn — paid for by
    # `rlm_plan_timeout_seconds`'s matching raise above.
    rlm_max_total_sub_agent_calls: int = Field(default=8, ge=1)

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
