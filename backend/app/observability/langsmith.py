"""LangSmith tracing wiring: turns the mandatory-per-ASSESSMENT.md tracing requirement into an
actual traced run per conversation turn, plus one best-effort live check so a bad key surfaces
at startup instead of as "no traces" silently discovered only once the demo video is recorded.

**Verified live, not assumed — and the assumption turned out to be wrong.** The obvious design
is "set `LANGSMITH_TRACING=true` and let LangChain's global, environment-variable-gated tracer
(`langsmith.utils.tracing_is_enabled`) instrument everything automatically" — no object to
construct or thread through `agents/`. `configure_langsmith` below still does exactly that, and
it is *not* wasted: a live run confirmed it correctly traces a bare `ChatOllama` call made
directly against the model (`main.py`'s own Ollama warm-up call showed up in LangSmith). But the
identical env-var configuration produced **zero** traces for any of the several LLM calls a real
chat turn's graph nodes make (Supervisor's routing call, two Response calls, two Validator
retries) — confirmed by querying the LangSmith API directly after the turn completed, not by a
missing entry in a UI. The env-based global tracer's auto-attach evidently does not reliably
reach a plain LangChain chat-model call made from inside a LangGraph node function (this
project's nodes are bare async functions the Pregel runtime schedules, not `Runnable`s chained
through `RunnableSequence` — nothing here threads an ambient `RunnableConfig` into
`llm/ollama_provider.py`'s calls, and evidently LangGraph's own node scheduling does not make
that happen for free either). `docs/ASSUMPTIONS_AND_TRADEOFFS.md` has the full investigation.

**The fix:** stop depending on implicit global state and attach an explicit
`LangChainTracer` as a callback on the graph invocation itself
(`build_tracing_callbacks`, consumed by `api/v1/chat.py`'s `config["callbacks"]`). LangGraph
*does* thread an explicitly-supplied `config["callbacks"]` through every node's execution — this
is the standard, documented way to attach tracing (or any other callback) to a compiled graph
run, and it is what actually produced a nested trace tree (one root `chat_turn` run with the
Supervisor/Retrieval/Response/Validator calls underneath it) in live verification. `configure_
langsmith` is kept anyway, both because it costs nothing and because it is still what any
LangChain code running *outside* this project's own graph invocation (a REPL, a notebook, a
future integration) would rely on.
"""

from __future__ import annotations

import asyncio
import os

import structlog
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import Client

from backend.app.core.config import Settings

logger = structlog.get_logger(__name__)


def configure_langsmith(settings: Settings) -> None:
    """Set the environment variables LangChain's global tracer reads, derived from this
    project's own validated `Settings` — never touches `os.environ` for anything tracing is not
    `Settings.langsmith_tracing`'s job to gate.

    Tracing is left off (nothing set) when disabled or unconfigured, rather than explicitly set
    to `"false"`, so a developer's own shell-level tracing configuration is never silently
    overridden by an unrelated app default they never asked this project to touch. See the
    module docstring for why this alone does **not** trace graph node calls —
    `build_tracing_callbacks` is what does that.
    """
    if not settings.langsmith_tracing:
        logger.info("langsmith_tracing_disabled")
        return
    if settings.langsmith_api_key is None:
        logger.warning("langsmith_tracing_requested_but_no_api_key")
        return

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    logger.info("langsmith_tracing_enabled", project=settings.langsmith_project)


def build_tracing_callbacks(settings: Settings) -> list[BaseCallbackHandler]:
    """Build the callback list `api/v1/chat.py` attaches to every graph invocation's `config`.

    Returns `[]` when tracing is disabled or unconfigured — an empty list is a valid, harmless
    `config["callbacks"]` value, so callers never need an `if` around this. Constructed once at
    startup (`main.py`'s lifespan calls this and stores the result on `app.state`), not per
    request: a `LangChainTracer` and its `Client` are safe to share across concurrent runs (each
    run gets its own run-id tree; nothing here is per-request mutable state), and reusing one
    avoids opening a fresh HTTP connection pool to LangSmith on every single chat turn.
    """
    if not settings.langsmith_tracing or settings.langsmith_api_key is None:
        return []
    client = Client(api_key=settings.langsmith_api_key.get_secret_value())
    return [LangChainTracer(project_name=settings.langsmith_project, client=client)]


async def verify_langsmith_connectivity(settings: Settings) -> bool:
    """Best-effort live authentication check, run once at startup so a bad key or unreachable
    endpoint is a warning in the startup log rather than a silent absence of traces. Never
    raises — consistent with every other external dependency `main.py`'s lifespan degrades
    rather than crashes on (Pinecone, MCP, Ollama): tracing is observability, not a load-bearing
    dependency, so a broken key must never be able to take the assistant itself down.

    `Client.list_projects` is a real authenticated call (not a local no-op), so a bad key or an
    unreachable `api.smith.langchain.com` both surface here, not three turns into the demo. It
    returns a lazy generator — nothing hits the network until the first item is pulled, so this
    explicitly consumes one via `next()` rather than merely constructing the generator. Run via
    `asyncio.to_thread` because the LangSmith SDK's `Client` is a synchronous `requests` client,
    not an async one.
    """
    api_key = settings.langsmith_api_key
    if not settings.langsmith_tracing or api_key is None:
        return False

    def _ping() -> None:
        client = Client(api_key=api_key.get_secret_value())
        next(client.list_projects(limit=1), None)

    try:
        await asyncio.to_thread(_ping)
    except Exception:
        logger.warning("langsmith_connectivity_check_failed", exc_info=True)
        return False
    logger.info("langsmith_connectivity_verified")
    return True
