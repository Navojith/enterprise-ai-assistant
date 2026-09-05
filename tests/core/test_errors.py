"""Tests for the exception hierarchy's FastAPI handlers.

Builds a throwaway app with the real handlers registered (`register_exception_handlers`) and
one route per failure mode, so these assert the actual JSON envelope and status code a client
receives — not just that the exception classes exist.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.core.errors import (
    RateLimitExceededError,
    ToolNotPermittedError,
    register_exception_handlers,
)


def _build_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/rate-limited")
    async def _rate_limited() -> None:
        raise RateLimitExceededError("Too many requests.", details={"retry_after_seconds": 5})

    @app.get("/tool-not-permitted")
    async def _tool_not_permitted() -> None:
        raise ToolNotPermittedError("Viewer role cannot call analytics tools.")

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("super secret internal stack trace detail")

    @app.post("/echo")
    async def _echo(payload: dict[str, int]) -> dict[str, int]:
        return payload

    return app


def test_app_error_maps_to_its_declared_status_and_envelope() -> None:
    client = TestClient(_build_app())

    response = client.get("/rate-limited")

    assert response.status_code == 429
    body = response.json()
    assert body["error"]["code"] == "rate_limit_exceeded"
    assert body["error"]["details"] == {"retry_after_seconds": 5}
    assert "correlation_id" in body


def test_tool_not_permitted_is_reported_as_forbidden() -> None:
    client = TestClient(_build_app())

    response = client.get("/tool-not-permitted")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "tool_not_permitted"


def test_unhandled_exception_never_leaks_internals() -> None:
    client = TestClient(_build_app(), raise_server_exceptions=False)

    response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "An unexpected error occurred."
    assert "secret internal stack trace" not in body["error"]["message"]


def test_request_validation_error_uses_the_same_envelope_shape() -> None:
    client = TestClient(_build_app())

    response = client.post("/echo", json={"count": "not-an-int"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "request_validation_error"
    assert body["error"]["details"]["errors"]
