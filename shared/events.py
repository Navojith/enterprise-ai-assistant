"""Typed activity events: the one contract between what a graph node does and what the
Agent Activity Panel renders.

ASSESSMENT.md requires the evaluator to "observe what the agent is doing internally" — current
agent state, active LangGraph node, tool calls, retrieval status, memory updates, validation
results, and final response generation. Rather than the frontend inferring any of that from
prose, every node emits one of these typed events through LangGraph's `StreamWriter`
(`get_stream_writer()`, called from inside the node — see `backend/app/agents/nodes/`), and the
SSE endpoint (`backend/app/api/v1/chat.py`) forwards each one to the browser essentially
unchanged. The panel is a direct view of the graph's own execution, not a parallel narration a
developer could let drift out of sync with what the graph actually did.

**Lives here, in a top-level `shared/` package, rather than under `backend/app/observability/`
where it was originally built** (`backend/app/observability/events.py` now just re-exports from
here for backward compatibility, since every backend node already imports it from that path).
Moved after a real bug, not a style preference: `frontend/app.py` and `frontend/api_client.py`
imported this module from `backend.app.observability.events` so the Agent Activity Panel could
render the exact shape a graph node emits rather than a hand-maintained duplicate — but that
import pulled in the *entire* `backend.app` package graph (FastAPI, LangGraph, Pinecone, Postgres
drivers, and `Settings`' required environment variables) just to reach two small `pydantic`
classes, and it broke outright the moment the frontend ran under `streamlit run` inside its own
Docker container (`ModuleNotFoundError: No module named 'backend'` — Streamlit's script runner
puts the script's own directory on `sys.path`, not the working directory `-m`-style invocations
rely on, so the absolute `backend.*` import had nothing to resolve against). Two independently
deployable processes reaching into each other's internals was the real defect; a `PYTHONPATH`
fix alone would have silenced the symptom without addressing it. This module is the actual
shared contract — depends on nothing but `pydantic` — so the frontend, the backend, and any
future consumer can all import it without depending on each other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ActivityEventType(StrEnum):
    """One member per bullet in ASSESSMENT.md's "Agent Activity Panel" requirement, plus
    `ERROR` for a degradation the panel should surface rather than hide."""

    NODE_ENTERED = "node_entered"
    TOOL_CALL = "tool_call"
    RETRIEVAL_STATUS = "retrieval_status"
    MEMORY_UPDATE = "memory_update"
    REASONING = "reasoning"
    VALIDATION_RESULT = "validation_result"
    ANSWER_DELTA = "answer_delta"
    ERROR = "error"


class ActivityEvent(BaseModel):
    """One entry in the Agent Activity Panel's timeline.

    `node` names the LangGraph node the event came from, so the panel can group and highlight
    "the currently active node" — a direct requirement of ASSESSMENT.md's Agent Activity Panel
    section. `data` carries the event-specific payload (retrieved chunk count, tool name and
    arguments, validation verdict, ...) as a plain dict rather than a union of payload types,
    because the panel only ever needs to display it, never branch its own logic on its shape.
    """

    event_type: ActivityEventType
    node: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
