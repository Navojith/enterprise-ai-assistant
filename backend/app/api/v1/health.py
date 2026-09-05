"""Liveness and readiness probes.

Liveness answers "is the process alive" and never touches a dependency — a transient
Postgres blip should not make an orchestrator kill and restart an otherwise-healthy process.
Readiness answers "can this instance serve real traffic right now" and does check Postgres,
because a graph that cannot reach its checkpointer store should not receive requests. This
split is the standard Kubernetes-style probe distinction and generalizes cleanly once Cycle 1
adds a Pinecone check and Cycle 3 adds an Ollama check to the same readiness aggregate.
"""

from __future__ import annotations

import asyncio

import psycopg
import structlog
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from backend.app.core.config import Settings, get_settings

logger = structlog.get_logger(__name__)

router = APIRouter()

_READINESS_TIMEOUT_SECONDS = 2.0


class LivenessResponse(BaseModel):
    status: str = "ok"


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, str]


def _to_psycopg_conninfo(database_url: str) -> str:
    """psycopg's own `connect()` speaks libpq conninfo strings, not SQLAlchemy's
    dialect+driver URL syntax — strip the `+psycopg` driver suffix before connecting."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


async def _check_database(database_url: str) -> str:
    """Open a short-lived connection and run a trivial query. Never raises — a failed
    dependency check is reported in the response body, not surfaced as a 500."""
    try:
        conn = await asyncio.wait_for(
            psycopg.AsyncConnection.connect(
                _to_psycopg_conninfo(database_url),
                connect_timeout=int(_READINESS_TIMEOUT_SECONDS),
            ),
            timeout=_READINESS_TIMEOUT_SECONDS,
        )
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()
        return "ok"
    except Exception as exc:  # noqa: BLE001 - a readiness check reports failure, never raises
        logger.warning("readiness_check_failed", dependency="database", error=str(exc))
        return f"error: {exc}"


@router.get("/health/live", response_model=LivenessResponse)
async def liveness() -> LivenessResponse:
    return LivenessResponse()


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(
    response: Response, settings: Settings = Depends(get_settings)
) -> ReadinessResponse:
    checks = {"database": await _check_database(settings.database_url)}
    healthy = all(value == "ok" for value in checks.values())
    response.status_code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if healthy else "not_ready", checks=checks)
