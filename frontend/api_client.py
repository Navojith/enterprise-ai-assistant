"""Typed HTTP client the Streamlit frontend uses to talk to the FastAPI backend.

Kept separate from `app.py` so the actual network and SSE-parsing logic is unit-testable
without a running Streamlit process or a running backend — `app.py` imports this module and
does nothing but render state (`docs/DECISIONS.md` §8's "production-grade code" bar applies to
the frontend too, not only `backend/`).

The frontend deliberately does **not** import anything from `backend.app` at all — including the
one typed contract both sides share, `ActivityEvent`, which now lives in the standalone
`shared/events.py` (see that module's docstring: this used to be an exception to this same rule,
importing `backend.app.observability.events` directly, and it was a real bug — that import
dragged in FastAPI/LangGraph/Pinecone/Postgres and `Settings`' required environment variables
just to reach two `pydantic` classes, and broke outright under `streamlit run` in Docker).
Reusing the shared type is still what lets the Agent Activity Panel render the exact same shape
a graph node emits rather than a hand-maintained duplicate that could quietly drift out of sync
with it — the fix removed the coupling, not the reuse. Everything else (auth, RBAC, error codes)
is addressed by field name only, deliberately loosely coupled to a second process reached only
over HTTP.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

from shared.events import ActivityEvent

_DONE_EVENT_NAME = "done"


class BackendError(Exception):
    """Base class for a frontend-side failure talking to the backend. Carries a plain,
    human-readable `message` the UI can show directly, mirroring the backend's own `AppError`
    shape without importing backend exception types — the two processes' failure domains are
    kept separate on purpose, since the frontend can fail in ways the backend never does (no
    process running at all) and vice versa."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BackendUnreachableError(BackendError):
    """The backend process could not be reached at all — connection refused, DNS, or timeout."""


class AuthenticationFailedError(BackendError):
    """Login was rejected, or a previously-issued token is no longer valid."""


class RateLimitedError(BackendError):
    """The caller's token bucket is empty. `retry_after_seconds` is `None` when the backend
    didn't report one — still a real rate limit, just without a precise wait time to display."""

    def __init__(self, message: str, *, retry_after_seconds: float | None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class Session:
    """An authenticated principal, as far as the frontend needs to know one: enough to attach
    a bearer token to every subsequent request and to show who is logged in as what role. RBAC
    itself is never re-implemented here — every permission check still happens at the backend's
    tool-execution boundary (`docs/DECISIONS.md` §6); the frontend only displays the role."""

    access_token: str
    username: str
    role: str


class ChatTurnComplete(BaseModel):
    """The chat endpoint's final SSE frame (`event: done`) — distinct from `ActivityEvent`
    because it is not something a graph node emitted, but `api/v1/chat.py`'s own sign-off after
    the graph finishes, carrying the correlation id a viewer could use to find this turn's logs
    or trace."""

    thread_id: str
    correlation_id: str | None = None


ChatStreamEvent = ActivityEvent | ChatTurnComplete


def _raise_for_error_response(response: httpx.Response) -> None:
    """Translate a non-2xx JSON error envelope (`core/errors.py::ErrorResponse`) from the
    backend into the matching typed frontend exception. Called only on responses the backend
    returned *before* opening the SSE stream (401/422/429/503) — a failure once the stream is
    already open arrives as an `error` `ActivityEvent` instead, per `api/v1/chat.py`'s own
    docstring on why a mid-stream failure can't become an HTTP status.
    """
    try:
        body = response.json()
        message = str(body.get("error", {}).get("message", response.text))
        details = body.get("error", {}).get("details") or {}
    except (json.JSONDecodeError, AttributeError):
        message = response.text or f"HTTP {response.status_code}"
        details = {}

    if response.status_code == httpx.codes.UNAUTHORIZED:
        raise AuthenticationFailedError(message)
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        raise RateLimitedError(message, retry_after_seconds=details.get("retry_after_seconds"))
    response.raise_for_status()


def login(base_url: str, username: str, password: str, *, timeout_seconds: float = 10.0) -> Session:
    """Exchange credentials for a session. Raises `BackendUnreachableError` if the backend
    process itself cannot be reached, or `AuthenticationFailedError` for a wrong username or
    password — the backend deliberately returns the identical error for both
    (`api/v1/auth.py`), so this client cannot and does not distinguish them either."""
    try:
        response = httpx.post(
            f"{base_url}/api/v1/auth/login",
            json={"username": username, "password": password},
            timeout=timeout_seconds,
        )
    except httpx.RequestError as exc:
        raise BackendUnreachableError(f"Could not reach the backend at {base_url}: {exc}") from exc

    if response.status_code != httpx.codes.OK:
        # Uniform with `stream_chat_turn`'s error handling — a wrong password surfaces the same
        # way any other backend-rejected request does, rather than a second, parallel mapping.
        _raise_for_error_response(response)
    body = response.json()
    return Session(access_token=body["access_token"], username=username, role=body["role"])


def _parse_sse_lines(lines: Iterator[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse decoded SSE text lines into `(event_name, data)` pairs.

    A pure generator over `Iterator[str]`, not `httpx.Response` directly — this is the one piece
    of real parsing logic in the frontend, so it is exercised directly in
    `tests/frontend/test_api_client.py` against plain lists of strings, no network involved.
    `api/v1/chat.py::_sse` only ever emits single-line `event:`/`data:` pairs (never a multi-line
    `data:` field or a `:` comment line), so this does not need to handle either.
    """
    pending_event: str | None = None
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if line.startswith("event:"):
            pending_event = line.removeprefix("event:").strip()
        elif line.startswith("data:") and pending_event is not None:
            yield pending_event, json.loads(line.removeprefix("data:").strip())
            pending_event = None
        # A blank line is SSE's frame separator; nothing to do — the pair was already yielded.


def _to_typed_event(event_name: str, data: dict[str, Any]) -> ChatStreamEvent:
    if event_name == _DONE_EVENT_NAME:
        return ChatTurnComplete.model_validate(data)
    return ActivityEvent.model_validate(data)


def stream_chat_turn(
    base_url: str,
    session: Session,
    *,
    thread_id: str,
    message: str,
    timeout_seconds: float = 240.0,
) -> Iterator[ChatStreamEvent]:
    """Open the SSE chat stream and yield typed events as they arrive.

    A generator, not a list: the whole point of streaming is that the caller (`app.py`'s script
    body) updates the Agent Activity Panel and the answer text as each event lands, matching
    ASSESSMENT.md's "display in real time" requirement, rather than blocking until the turn
    finishes and rendering everything at once. `timeout_seconds` defaults well above
    `Settings.rlm_plan_timeout_seconds` (180s, `docs/DECISIONS.md` §9) since a research-routed
    turn is the slowest path this client will ever wait on.
    """
    try:
        with httpx.stream(
            "POST",
            f"{base_url}/api/v1/chat/stream",
            json={"thread_id": thread_id, "message": message},
            headers={"Authorization": f"Bearer {session.access_token}"},
            timeout=timeout_seconds,
        ) as response:
            if response.status_code != httpx.codes.OK:
                response.read()  # the error body is JSON, not an SSE stream — must be read first
                _raise_for_error_response(response)
            yield from (
                _to_typed_event(name, data)
                for name, data in _parse_sse_lines(response.iter_lines())
            )
    except httpx.RequestError as exc:
        raise BackendUnreachableError(
            f"Lost connection to the backend at {base_url}: {exc}"
        ) from exc
