"""Integration tests for the streaming chat endpoint.

Built against a throwaway app with a fake compiled graph on `app.state`, not the real
`create_app()` — same reasoning as `tests/api/test_deps.py`: this exercises the endpoint's own
wiring (auth, the `graph is None` guard, SSE framing) without needing a real Postgres
checkpointer, Pinecone, or Ollama. The full graph was exercised live end to end; see
docs/PROGRESS.md's session log.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.v1.auth import router as auth_router
from backend.app.api.v1.chat import router as chat_router
from backend.app.core.config import get_settings
from backend.app.core.errors import register_exception_handlers
from backend.app.core.security import rate_limit
from backend.app.core.security.rate_limit import _RefillResult
from backend.app.observability.events import ActivityEvent, ActivityEventType


class _FakeGraph:
    def __init__(self, events: list[ActivityEvent]) -> None:
        self._events = events
        self.received_config: dict[str, Any] | None = None

    async def astream(
        self,
        input_state: dict[str, Any],
        config: dict[str, Any] | None = None,
        *,
        context: Any = None,
        stream_mode: str | None = None,
    ) -> AsyncIterator[ActivityEvent]:
        self.received_config = config
        for event in self._events:
            yield event


def _build_app(*, graph: _FakeGraph | None) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")
    app.state.graph = graph
    app.state.graph_context = object() if graph is not None else None
    return app


@pytest.fixture(autouse=True)
def _configure_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("JWT_SECRET_KEY", "test-signing-key-for-chat-endpoint-tests-32-chars")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _allow_every_rate_limit_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rate_limit,
        "_consume_token",
        AsyncMock(
            return_value=_RefillResult(allowed=True, tokens_after=4.0, retry_after_seconds=None)
        ),
    )


def _token_for(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    token: str = response.json()["access_token"]
    return token


def test_an_unauthenticated_request_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TestClient(_build_app(graph=_FakeGraph([])))

    response = client.post("/api/v1/chat/stream", json={"thread_id": "t1", "message": "hi"})

    assert response.status_code == 401


def test_a_viewer_can_open_a_chat_stream() -> None:
    events = [
        ActivityEvent(
            event_type=ActivityEventType.NODE_ENTERED, node="supervisor", message="Routing."
        ),
        ActivityEvent(event_type=ActivityEventType.ANSWER_DELTA, node="response", message="Hi"),
    ]
    graph = _FakeGraph(events)
    client = TestClient(_build_app(graph=graph))
    token = _token_for(client, "viewer", "ViewerPass123!")

    response = client.post(
        "/api/v1/chat/stream",
        json={"thread_id": "t1", "message": "Hello"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: node_entered" in response.text
    assert "event: answer_delta" in response.text
    assert "event: done" in response.text
    assert graph.received_config is not None
    assert graph.received_config["configurable"] == {"thread_id": "t1"}
    assert graph.received_config["metadata"]["principal_role"] == "viewer"
    assert graph.received_config["tags"] == ["role:viewer"]


def test_the_thread_id_and_principal_are_passed_into_the_graph() -> None:
    graph = _FakeGraph([])
    client = TestClient(_build_app(graph=graph))
    token = _token_for(client, "analyst", "AnalystPass123!")

    client.post(
        "/api/v1/chat/stream",
        json={"thread_id": "thread-42", "message": "What is our SLA?"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert graph.received_config is not None
    assert graph.received_config["configurable"] == {"thread_id": "thread-42"}
    assert graph.received_config["metadata"]["thread_id"] == "thread-42"
    assert graph.received_config["metadata"]["principal_username"] == "analyst"


def test_an_unavailable_graph_returns_a_graceful_503() -> None:
    client = TestClient(_build_app(graph=None))
    token = _token_for(client, "viewer", "ViewerPass123!")

    response = client.post(
        "/api/v1/chat/stream",
        json={"thread_id": "t1", "message": "hi"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "graph_unavailable"


def test_an_empty_message_is_rejected_by_request_validation() -> None:
    client = TestClient(_build_app(graph=_FakeGraph([])))
    token = _token_for(client, "viewer", "ViewerPass123!")

    response = client.post(
        "/api/v1/chat/stream",
        json={"thread_id": "t1", "message": ""},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422
