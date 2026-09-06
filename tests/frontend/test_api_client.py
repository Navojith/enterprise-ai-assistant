"""Tests for the frontend's typed backend client.

`_parse_sse_lines`, `_to_typed_event` and `_raise_for_error_response` are pure functions split
out specifically so they are testable without a network or a running backend — the same
pure-logic/IO-shell split `backend/app/retrieval/reranker.py` and `backend/app/core/security/
rate_limit.py` use. `login` and `stream_chat_turn` (the IO shells) are exercised by monkeypatching
`httpx.post`/`httpx.stream` directly, rather than a real socket — this project has no interest in
testing httpx itself, only that this client wires it correctly.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from frontend.api_client import (
    AuthenticationFailedError,
    BackendUnreachableError,
    ChatTurnComplete,
    RateLimitedError,
    Session,
    _parse_sse_lines,
    _raise_for_error_response,
    _to_typed_event,
    login,
    stream_chat_turn,
)
from shared.events import ActivityEvent, ActivityEventType


def test_parse_sse_lines_pairs_event_and_data_lines_across_frames() -> None:
    lines = [
        "event: node_entered",
        'data: {"a": 1}',
        "",
        "event: done",
        'data: {"b": 2}',
        "",
    ]

    parsed = list(_parse_sse_lines(iter(lines)))

    assert parsed == [("node_entered", {"a": 1}), ("done", {"b": 2})]


def test_parse_sse_lines_ignores_a_data_line_with_no_preceding_event() -> None:
    parsed = list(_parse_sse_lines(iter(['data: {"orphan": true}'])))

    assert parsed == []


def test_to_typed_event_builds_an_activity_event_for_a_regular_event_name() -> None:
    event = _to_typed_event(
        "retrieval_status",
        {
            "event_type": "retrieval_status",
            "node": "retrieval",
            "message": "Found 3 chunks.",
            "data": {},
            "timestamp": "2026-01-01T00:00:00Z",
        },
    )

    assert isinstance(event, ActivityEvent)
    assert event.event_type == ActivityEventType.RETRIEVAL_STATUS
    assert event.node == "retrieval"


def test_to_typed_event_builds_a_chat_turn_complete_for_the_done_event() -> None:
    event = _to_typed_event("done", {"thread_id": "t1", "correlation_id": "c1"})

    assert isinstance(event, ChatTurnComplete)
    assert event.thread_id == "t1"


def test_raise_for_error_response_maps_401_to_authentication_failed() -> None:
    response = httpx.Response(401, json={"error": {"message": "Invalid credentials."}})

    with pytest.raises(AuthenticationFailedError, match="Invalid credentials"):
        _raise_for_error_response(response)


def test_raise_for_error_response_maps_429_to_rate_limited_with_retry_after() -> None:
    response = httpx.Response(
        429,
        json={
            "error": {
                "message": "Rate limit exceeded.",
                "details": {"retry_after_seconds": 12.5},
            }
        },
    )

    with pytest.raises(RateLimitedError) as excinfo:
        _raise_for_error_response(response)
    assert excinfo.value.retry_after_seconds == 12.5


def test_raise_for_error_response_reraises_other_status_codes() -> None:
    response = httpx.Response(
        503,
        json={"error": {"message": "unavailable"}},
        request=httpx.Request("POST", "http://backend.test/x"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        _raise_for_error_response(response)


def test_login_returns_a_session_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {"username": "viewer", "password": "ViewerPass123!"}
        return httpx.Response(200, json={"access_token": "tok", "role": "viewer"})

    monkeypatch.setattr(httpx, "post", fake_post)

    session = login("http://backend.test", "viewer", "ViewerPass123!")

    assert session == Session(access_token="tok", username="viewer", role="viewer")


def test_login_raises_authentication_failed_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(401))

    with pytest.raises(AuthenticationFailedError):
        login("http://backend.test", "viewer", "wrong-password")


def test_login_raises_backend_unreachable_on_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(*_args: Any, **_kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(BackendUnreachableError):
        login("http://backend.test", "viewer", "ViewerPass123!")


class _FakeStreamResponse:
    def __init__(self, status_code: int, lines: list[str]) -> None:
        self.status_code = status_code
        self._lines = lines

    def read(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"error": {"message": "denied"}}

    @property
    def text(self) -> str:
        return "denied"

    def iter_lines(self) -> Iterator[str]:
        return iter(self._lines)


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    def __enter__(self) -> _FakeStreamResponse:
        return self._response

    def __exit__(self, *_exc_info: object) -> None:
        return None


def test_stream_chat_turn_yields_typed_events_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        "event: node_entered",
        'data: {"event_type": "node_entered", "node": "supervisor", "message": "Routing.", '
        '"data": {}, "timestamp": "2026-01-01T00:00:00Z"}',
        "",
        "event: done",
        'data: {"thread_id": "t1", "correlation_id": "c1"}',
        "",
    ]
    monkeypatch.setattr(
        httpx,
        "stream",
        lambda *a, **k: _FakeStreamContext(_FakeStreamResponse(200, lines)),
    )
    session = Session(access_token="tok", username="viewer", role="viewer")

    events = list(stream_chat_turn("http://backend.test", session, thread_id="t1", message="hi"))

    assert isinstance(events[0], ActivityEvent)
    assert events[0].node == "supervisor"
    assert isinstance(events[1], ChatTurnComplete)
    assert events[1].thread_id == "t1"


def test_stream_chat_turn_raises_typed_error_for_a_non_200_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "stream",
        lambda *a, **k: _FakeStreamContext(_FakeStreamResponse(401, [])),
    )
    session = Session(access_token="expired", username="viewer", role="viewer")

    with pytest.raises(AuthenticationFailedError):
        list(stream_chat_turn("http://backend.test", session, thread_id="t1", message="hi"))
