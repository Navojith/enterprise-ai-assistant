"""FastAPI application factory and process lifespan.

Cycle 0 wires configuration, structured logging, a correlation-id + access-log middleware,
the exception hierarchy, and the liveness/readiness health check. Later cycles extend
`lifespan` to open the Postgres checkpointer pool, create the Pinecone client, warm up Ollama,
and start the MCP client session — each extension point is marked with the cycle that fills
it in, rather than left to be rediscovered.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import Response

from backend.app.api.v1.health import router as health_router
from backend.app.core.config import get_settings
from backend.app.core.errors import register_exception_handlers
from backend.app.core.logging import (
    CORRELATION_ID_HEADER,
    bind_correlation_id,
    configure_logging,
    get_logger,
)

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    logger.info("startup", environment=settings.environment, ollama_model=settings.ollama_model)

    # Cycle 1: create the Pinecone client (dense + sparse indexes) and store it on app.state.
    # Cycle 3: open the AsyncPostgresSaver checkpointer pool and issue an Ollama warm-up call.
    # Cycle 4: start the MCP client session.
    yield

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

    return app


app = create_app()
