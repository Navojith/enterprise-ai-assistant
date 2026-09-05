"""FastAPI application factory and process lifespan.

Cycle 0 wired configuration, structured logging, a correlation-id + access-log middleware, the
exception hierarchy, and the liveness/readiness health check. Cycle 3 adds the rest of
`lifespan`: the Pinecone client and index check, an Ollama warm-up call, the
`AsyncPostgresSaver` checkpointer pool, and the compiled LangGraph graph — stored on `app.state`
because, unlike every other cached-factory singleton in this codebase (`get_settings`,
`get_engine`, `get_pinecone_store`), building them requires `await`, which a plain `lru_cache`
factory cannot do. Cycle 4 extends this further to start the MCP client session.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from backend.app.agents.context import GraphContext
from backend.app.agents.graph import build_graph
from backend.app.api.v1.auth import router as auth_router
from backend.app.api.v1.chat import router as chat_router
from backend.app.api.v1.health import router as health_router
from backend.app.core.config import get_settings
from backend.app.core.db import create_all_tables
from backend.app.core.errors import register_exception_handlers
from backend.app.core.logging import (
    CORRELATION_ID_HEADER,
    bind_correlation_id,
    configure_logging,
    get_logger,
)
from backend.app.llm.chain import FallbackChain
from backend.app.llm.ollama_provider import OllamaProvider
from backend.app.retrieval.pinecone_store import PineconeStore, get_pinecone_store

logger = get_logger(__name__)

_CHECKPOINTER_STARTUP_TIMEOUT_SECONDS = 10.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    logger.info("startup", environment=settings.environment, ollama_model=settings.ollama_model)

    # Best-effort: a Postgres blip here shouldn't stop the process from serving liveness/
    # readiness (which will itself report the database as unreachable) — see the retry-free
    # graceful-degradation pattern in api/v1/health.py.
    try:
        await create_all_tables()
    except Exception:
        logger.warning("startup_table_creation_failed", exc_info=True)

    # Cycle 1's Pinecone client, initialized once here rather than lazily on first request, so a
    # missing/unreachable index surfaces at startup logs instead of on a user's first turn. Both
    # "no PINECONE_API_KEY configured" (raised by the constructor itself) and "configured but
    # unreachable" (raised by `ensure_indexes`) degrade to `pinecone_store = None` the same way —
    # `retrieval_node` treats a `None` store exactly like a live retrieval failure.
    pinecone_store: PineconeStore | None
    try:
        pinecone_store = get_pinecone_store()
        await pinecone_store.ensure_indexes()
    except Exception:
        logger.warning("startup_pinecone_unavailable", exc_info=True)
        pinecone_store = None

    llm_provider = FallbackChain(
        [OllamaProvider(settings)],
        failure_threshold=settings.llm_circuit_breaker_failure_threshold,
        cooldown_seconds=settings.llm_circuit_breaker_cooldown_seconds,
    )
    try:
        # Loads the model into VRAM before the first real request — see
        # docs/ASSUMPTIONS_AND_TRADEOFFS.md trade-off 13 on why a cold first call is otherwise
        # dramatically slower than every call after it.
        async for _ in llm_provider.astream([HumanMessage(content="Reply with OK.")]):
            break
        logger.info("ollama_warmup_succeeded")
    except Exception:
        logger.warning("ollama_warmup_failed", exc_info=True)

    # A second, independent connection to the same Postgres instance from `core/db.py`'s
    # SQLAlchemy engine — `AsyncPostgresSaver` owns its own pool and schema
    # (docs/ASSUMPTIONS_AND_TRADEOFFS.md assumption 6). `autocommit`/`prepare_threshold`/
    # `row_factory` mirror exactly what `AsyncPostgresSaver.from_conn_string` sets up internally
    # for a single connection — required here explicitly because a pool, not a single
    # connection, is what a concurrent FastAPI app needs.
    #
    # Unlike the two blocks above, a failure here degrades the graph itself to unavailable
    # (`app.state.graph = None`) rather than a lesser mode — there is no meaningful way to run
    # the assistant without its checkpointer. `api/v1/chat.py` turns that `None` into a clean
    # 503 (`GraphUnavailableError`) instead of every request hitting an `AttributeError`, and
    # liveness/readiness stay servable either way so an orchestrator sees "not ready", not "dead".
    checkpoint_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None
    app.state.graph = None
    app.state.graph_context = None
    try:
        checkpoint_pool = AsyncConnectionPool(
            settings.psycopg_dsn,
            # `connection_class` fixes the pool's *static* row type to `DictRow` — the
            # `row_factory` kwarg below only affects rows at runtime, and without this mypy sees
            # a pool of tuple-row connections (psycopg's default), which `AsyncPostgresSaver`
            # correctly rejects.
            connection_class=AsyncConnection[DictRow],
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await asyncio.wait_for(
            checkpoint_pool.open(), timeout=_CHECKPOINTER_STARTUP_TIMEOUT_SECONDS
        )
        checkpointer = AsyncPostgresSaver(checkpoint_pool)
        await asyncio.wait_for(checkpointer.setup(), timeout=_CHECKPOINTER_STARTUP_TIMEOUT_SECONDS)
        app.state.graph_context = GraphContext(
            llm=llm_provider, pinecone_store=pinecone_store, settings=settings
        )
        app.state.graph = build_graph(
            checkpointer, max_validator_retries=settings.max_validator_retries
        )
    except Exception:
        logger.warning("startup_graph_unavailable", exc_info=True)

    # Cycle 4: start the MCP client session.
    yield

    if checkpoint_pool is not None:
        await checkpoint_pool.close()
    logger.info("shutdown")


def create_app() -> FastAPI:
    """Application factory.

    Building the app in a function, rather than a module-level `app = FastAPI()`, keeps
    construction free of import-time side effects and lets tests build an app against
    different settings.
    """
    app = FastAPI(
        title="Enterprise AI Assistant",
        description="Conversational assistant over internal documents with RBAC-gated tools.",
        version="0.1.0",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    @app.middleware("http")
    async def correlation_id_and_access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = bind_correlation_id(request.headers.get(CORRELATION_ID_HEADER))
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.error(
                "request_failed", method=request.method, path=request.url.path, exc_info=True
            )
            raise
        duration_ms = (time.perf_counter() - start) * 1000
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        return response

    app.include_router(health_router, prefix="/api/v1", tags=["health"])
    app.include_router(auth_router, prefix="/api/v1", tags=["auth"])
    app.include_router(chat_router, prefix="/api/v1", tags=["chat"])

    return app


app = create_app()
