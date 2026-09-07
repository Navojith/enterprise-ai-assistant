"""Streamlit chat UI: multi-turn conversation, streaming answers, and the Agent Activity Panel.

ASSESSMENT.md asks for "a lightweight chat interface" where "UI beauty is not important" and
"focus on functionality and transparency" — this file is deliberately a thin rendering layer
over `api_client.py`'s typed HTTP/SSE client, not a second place business logic lives. Every
fact the panel shows (current node, tool calls, retrieval status, memory updates, validation
results) is a direct render of an `ActivityEvent` the backend's own graph emitted
(`shared/events.py` — the frontend imports this shared contract, not `backend.app` itself; see
that module's docstring for why) — this file never infers or narrates what the agent is doing,
only displays it, so the panel can never drift out of sync with what actually happened.

Run with `python -m streamlit run frontend/app.py` (see `docs/SETUP.md`). `BACKEND_URL` is read from the
environment (default `http://localhost:8000`) rather than hardcoded, so the same file works
against a locally-run backend or one reachable at another address without editing code.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import streamlit as st

from frontend.api_client import (
    AuthenticationFailedError,
    BackendError,
    BackendUnreachableError,
    ChatTurnComplete,
    RateLimitedError,
    Session,
    login,
    stream_chat_turn,
)
from shared.events import ActivityEvent, ActivityEventType

DEFAULT_BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")

# Documented, not secret — the same three accounts `docs/SETUP.md` publishes for curl-based
# testing. Offering them as one-click buttons is what makes switching roles fast enough to
# demonstrate RBAC live (ASSESSMENT.md's own evaluation criteria) without retyping credentials.
_DEMO_USERS: dict[str, tuple[str, str]] = {
    "Viewer": ("viewer", "ViewerPass123!"),
    "Analyst": ("analyst", "AnalystPass123!"),
    "Administrator": ("admin", "AdminPass123!"),
}

_NODE_ICONS: dict[str, str] = {
    "guardrail": "🛡️",
    "supervisor": "🧭",
    "retrieval": "📚",
    "tools": "🔧",
    "research": "🔬",
    "response": "✍️",
    "validator": "✅",
    "graph": "⚠️",
}

# Streamed token-by-token into the answer/reasoning placeholders, not the step-by-step log —
# logging every individual token would turn the Agent Activity Panel into noise.
_STREAMED_TEXT_EVENTS = frozenset(
    {ActivityEventType.ANSWER_DELTA, ActivityEventType.REASONING}
)


def _init_session_state() -> None:
    st.session_state.setdefault("session", None)
    st.session_state.setdefault("thread_id", None)
    st.session_state.setdefault("chat_history", [])
    # `(turn_index, ActivityEvent)` pairs, kept across turns (only cleared by "New conversation")
    # so an evaluator can scroll back through what earlier turns did, not just the latest one.
    st.session_state.setdefault("activity_log", [])
    st.session_state.setdefault("backend_url", DEFAULT_BACKEND_URL)


def _start_new_conversation() -> None:
    st.session_state.thread_id = uuid.uuid4().hex
    st.session_state.chat_history = []
    st.session_state.activity_log = []


def _log_in(username: str, password: str) -> None:
    try:
        session = login(st.session_state.backend_url, username, password)
    except AuthenticationFailedError:
        st.error("Invalid username or password.")
        return
    except BackendUnreachableError as exc:
        st.error(str(exc))
        return
    st.session_state.session = session
    _start_new_conversation()
    st.rerun()


def _log_out() -> None:
    st.session_state.session = None
    st.session_state.thread_id = None
    st.session_state.chat_history = []
    st.session_state.activity_log = []


def _backend_status(base_url: str) -> tuple[bool, str]:
    """Best-effort liveness probe for the sidebar — never raises, since a frontend that cannot
    reach the backend yet should still render a login form and a clear status, not a stack
    trace. Deliberately calls `/health/live`, not `/health/ready`: the sidebar cares whether the
    *process* is reachable at all, not whether its Postgres dependency is currently healthy.
    """
    try:
        response = httpx.get(f"{base_url}/api/v1/health/live", timeout=2.0)
        return response.status_code == httpx.codes.OK, "reachable"
    except httpx.RequestError as exc:
        return False, str(exc)


def _render_login_screen() -> None:
    st.title("🏦 Enterprise AI Assistant")
    st.caption(
        "Sign in to start a conversation. See docs/SETUP.md for account details."
    )

    reachable, detail = _backend_status(st.session_state.backend_url)
    if not reachable:
        st.warning(
            f"Backend not reachable at {st.session_state.backend_url} ({detail}). "
            "Start it with `uvicorn backend.app.main:app ...` — see docs/SETUP.md."
        )

    st.subheader("Quick demo login")
    columns = st.columns(len(_DEMO_USERS))
    for column, (label, (username, password)) in zip(
        columns, _DEMO_USERS.items(), strict=True
    ):
        if column.button(f"Log in as {label}", use_container_width=True):
            _log_in(username, password)

    with st.expander("Log in with different credentials"), st.form("manual_login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Log in") and username and password:
            _log_in(username, password)


def _render_sidebar(session: Session) -> None:
    with st.sidebar:
        st.markdown(f"**Signed in as** `{session.username}`")
        st.markdown(f"**Role** `{session.role}`")
        st.caption(f"Conversation `{st.session_state.thread_id}`")
        if st.button("🆕 New conversation", use_container_width=True):
            _start_new_conversation()
            st.rerun()
        if st.button("🚪 Log out", use_container_width=True):
            _log_out()
            st.rerun()
        st.divider()
        reachable, _detail = _backend_status(st.session_state.backend_url)
        st.caption(f"Backend: {'🟢 reachable' if reachable else '🔴 unreachable'}")
        st.caption(st.session_state.backend_url)


def _format_event_data(data: dict[str, Any]) -> str:
    """A short, single-line rendering of an event's auxiliary payload — chunk ids, tool
    arguments, route choice — for the caption under a log line. Deliberately truncated rather
    than pretty-printed JSON, since the Agent Activity Panel is a running log, not an inspector.
    """
    parts = [f"{key}={value!r}" for key, value in data.items()]
    rendered = ", ".join(parts)
    return rendered if len(rendered) <= 160 else f"{rendered[:157]}..."


def _render_activity_event(container: Any, event: ActivityEvent) -> None:
    icon = _NODE_ICONS.get(event.node, "•")
    line = f"{icon} **{event.node}** — {event.message}"
    if event.event_type == ActivityEventType.ERROR:
        container.error(line)
    elif (
        event.event_type == ActivityEventType.VALIDATION_RESULT
        and event.data.get("passed") is False
    ):
        container.warning(line)
    else:
        container.markdown(line)
    if event.data:
        container.caption(_format_event_data(event.data))


def _render_activity_panel(events: list[tuple[int, ActivityEvent]]) -> None:
    st.subheader("🔎 Agent Activity Panel")
    st.caption(
        "A direct view of the LangGraph run — every line below is something a node itself reported."
    )
    if not events:
        st.info("Send a message to see the graph's execution here.")
        return
    with st.container(height=520):
        last_turn = events[0][0]
        for turn_index, event in events:
            if turn_index != last_turn:
                st.divider()
                last_turn = turn_index
            if event.event_type not in _STREAMED_TEXT_EVENTS:
                _render_activity_event(st, event)


def _render_chat_history() -> None:
    for turn in st.session_state.chat_history:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])


def _run_turn(session: Session, prompt: str) -> None:
    """Stream one chat turn, updating the answer and the Agent Activity Panel live as events
    arrive, then commit the finished turn to persisted session state and rerun so the next
    script pass renders everything — including this turn — from that one source of truth
    (`_render_chat_history` / `_render_activity_panel`), rather than leaving two code paths that
    could render a turn differently depending on whether it just streamed or was replayed.
    """
    st.session_state.chat_history.append({"role": "user", "content": prompt})
    turn_index = len(st.session_state.chat_history)

    chat_col, panel_col = st.columns([2, 1])
    with chat_col:
        with st.chat_message("user"):
            st.markdown(prompt)
        answer_placeholder = st.chat_message("assistant").empty()
    with panel_col:
        st.subheader("🔎 Agent Activity Panel")
        status_placeholder = st.empty()
        log_container = st.container(height=460)

    answer_text = ""
    error_message: str | None = None
    try:
        for event in stream_chat_turn(
            st.session_state.backend_url,
            session,
            thread_id=st.session_state.thread_id,
            message=prompt,
        ):
            if isinstance(event, ChatTurnComplete):
                break
            st.session_state.activity_log.append((turn_index, event))
            if event.event_type == ActivityEventType.NODE_ENTERED:
                icon = _NODE_ICONS.get(event.node, "•")
                status_placeholder.info(f"**Active node:** {icon} {event.node}")
                if event.node == "response":
                    # The Validator can send the turn back to Response for a bounded number of
                    # retries (`docs/ARCHITECTURE.md`'s Validator -> Response loop) — each
                    # re-entry starts a fresh draft, so the abandoned one must not stay
                    # concatenated onto the front of what replaces it. Verified live: without
                    # this reset, a retried turn showed the rejected draft's text immediately
                    # followed by the accepted one, both in the same bubble.
                    answer_text = ""
                    answer_placeholder.empty()
            if event.event_type == ActivityEventType.ANSWER_DELTA:
                answer_text += event.message
                answer_placeholder.markdown(answer_text)
            elif event.event_type == ActivityEventType.ERROR:
                error_message = event.message
                _render_activity_event(log_container, event)
            else:
                _render_activity_event(log_container, event)
    except RateLimitedError as exc:
        retry = (
            f" Try again in {exc.retry_after_seconds:.0f}s."
            if exc.retry_after_seconds
            else ""
        )
        error_message = f"{exc.message}{retry}"
    except BackendError as exc:
        error_message = exc.message

    status_placeholder.empty()
    if not answer_text:
        answer_text = (
            f"⚠️ {error_message}" if error_message else "⚠️ No response was generated."
        )
        answer_placeholder.error(answer_text)
    elif error_message:
        answer_text = f"{answer_text}\n\n⚠️ {error_message}"

    st.session_state.chat_history.append({"role": "assistant", "content": answer_text})
    st.rerun()


def _render_chat_screen(session: Session) -> None:
    _render_sidebar(session)
    st.title("🏦 Enterprise AI Assistant")

    prompt = st.chat_input(
        "Ask about policies, incidents, runbooks, or product specs..."
    )
    if prompt:
        _run_turn(session, prompt)
        return

    chat_col, panel_col = st.columns([2, 1])
    with chat_col:
        _render_chat_history()
    with panel_col:
        _render_activity_panel(st.session_state.activity_log)


def main() -> None:
    st.set_page_config(
        page_title="Enterprise AI Assistant", page_icon="🏦", layout="wide"
    )
    _init_session_state()

    session: Session | None = st.session_state.session
    if session is None:
        _render_login_screen()
    else:
        _render_chat_screen(session)


main()
