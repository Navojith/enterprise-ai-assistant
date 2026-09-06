"""Streaming chat endpoint: the graph's own execution *is* the Agent Activity Panel's feed.

Every `ActivityEvent` a node emits via LangGraph's `StreamWriter` (see `observability/events.py`
and `agents/nodes/`) is forwarded to the browser as one Server-Sent Event, in the order the graph
actually produced it — the panel is a direct view of execution, not a narration a developer could
let drift out of sync with what the graph did. `stream_mode="custom"` is what surfaces exactly
those writer calls and nothing else (no LangGraph-internal bookkeeping to filter back out).

`thread_id` is the caller's own conversation identifier, not server-generated: the same id
across requests resumes prior turns via the `AsyncPostgresSaver` checkpointer
(`agents/graph.py`), which is the entire mechanism behind ASSESSMENT.md's "maintain conversation
context" requirement — there is no separate session store to keep in sync with it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from backend.app.agents.context import GraphContext
from backend.app.api.deps import require_permission
from backend.app.core.errors import AppError, GraphUnavailableError
from backend.app.core.logging import get_correlation_id
from backend.app.core.security.rbac import Permission, Principal
from backend.app.guardrails.validators import validate_user_message
from backend.app.observability.events import ActivityEvent, ActivityEventType

logger = structlog.get_logger(__name__)

router = APIRouter()

# Built once at import time, not inline in the route signature — see tests/api/test_deps.py for
# the same pattern; a bare `Depends(require_permission(Permission.CHAT))` default would call
# the factory on every import-time signature evaluation (flake8-bugbear B008).
_require_chat = require_permission(Permission.CHAT)


class ChatRequest(BaseModel):
    thread_id: str = Field(
        min_length=1,
        max_length=200,
        description="Caller-chosen conversation id. Reusing it resumes prior turns.",
    )
    message: str = Field(min_length=1, max_length=4000)


def _sse(event: str, data: dict[str, Any]) -> str:
    # SSE frames are newline-delimited; `default=str` covers the one non-JSON-native type in
    # ActivityEvent (`timestamp: datetime`) without a bespoke encoder.
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _stream_turn(
    request: Request, principal: Principal, payload: ChatRequest
) -> AsyncIterator[str]:
    graph = request.app.state.graph
    graph_context: GraphContext = request.app.state.graph_context
    correlation_id = get_correlation_id()
    config = {
        "configurable": {"thread_id": payload.thread_id},
        # LangSmith run metadata (ASSESSMENT.md's "trace every conversation" requirement) —
        # `metadata`/`tags` are the standard `RunnableConfig` keys LangGraph forwards onto every
        # run in the trace tree, and `run_name` is what names the top-level run in the LangSmith
        # UI instead of a bare "LangGraph". `correlation_id` is what a reader joins a trace back
        # to this request's structured logs by (`core/logging.py`); `role` is never authorization
        # itself (docs/DECISIONS.md §6) — it is here purely so a trace explorer can filter by it.
        "metadata": {
            "correlation_id": correlation_id,
            "thread_id": payload.thread_id,
            "principal_username": principal.username,
            "principal_role": principal.role.value,
        },
        "tags": [f"role:{principal.role.value}"],
        "run_name": "chat_turn",
        # Explicit, not the env-var-only global tracer — `observability/langsmith.py`'s module
        # docstring has the live-verified reason: the global tracer alone traced a bare `ChatOllama`
        # call but produced zero traces for any LLM call a graph node made. `getattr` with a `[]`
        # default so a bare `FastAPI()` test app that never ran the real lifespan (`tests/api/
        # test_chat.py`) doesn't need to stub this attribute just to exercise the endpoint.
        "callbacks": getattr(request.app.state, "langsmith_callbacks", []),
    }
    input_state = {
        "messages": [HumanMessage(content=payload.message)],
        "principal_username": principal.username,
        "principal_role": principal.role.value,
    }

    try:
        async for event in graph.astream(
            input_state, config=config, context=graph_context, stream_mode="custom"
        ):
            # `stream_mode="custom"` yields exactly what a node passed to its `StreamWriter` —
            # every node in this graph only ever writes an `ActivityEvent` (see `agents/nodes/`).
            if not isinstance(event, ActivityEvent):
                logger.warning("chat_stream_unexpected_event_type", event_type=type(event).__name__)
                continue
            yield _sse(event.event_type.value, event.model_dump(mode="json"))
    except AppError as exc:
        # Headers are already flushed by the time an SSE stream is open, so a mid-stream failure
        # cannot become an HTTP error status — it becomes one more event instead, letting the
        # frontend show it in the Agent Activity Panel like anything else that went wrong.
        logger.error("chat_stream_failed", code=exc.code, error=exc.message, exc_info=True)
        yield _sse(
            ActivityEventType.ERROR.value,
            {
                "event_type": "error",
                "node": "graph",
                "message": exc.message,
                "data": {"code": exc.code},
            },
        )
        return

    yield _sse("done", {"thread_id": payload.thread_id, "correlation_id": get_correlation_id()})


@router.post("/chat/stream")
async def chat_stream(
    request: Request,
    payload: ChatRequest,
    principal: Principal = Depends(_require_chat),
) -> StreamingResponse:
    # Shape validation (guardrails/validators.py), not intent — whitespace-only, control
    # characters, pathological repetition. Deliberately *not* prompt-injection screening: that
    # runs inside the graph itself (`agents/nodes/guardrail.py`), because a block there must be
    # observable in the Agent Activity Panel and a LangSmith trace, which nothing outside
    # `graph.astream()` ever is. Checked here, before the stream opens, for the same reason the
    # graph-availability check below is: a normal 422/503 JSON error through the standard
    # exception handler, not an SSE frame sent after headers already promised a
    # text/event-stream body.
    validate_user_message(payload.message)

    if request.app.state.graph is None:
        raise GraphUnavailableError(
            "The conversation graph is not available — its checkpointer failed to initialize "
            "at startup. Check that Postgres is reachable and retry."
        )
    return StreamingResponse(
        _stream_turn(request, principal, payload),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
