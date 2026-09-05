"""Typed activity events: the one contract between what a graph node does and what the
Agent Activity Panel renders.

ASSESSMENT.md requires the evaluator to "observe what the agent is doing internally" — current
agent state, active LangGraph node, tool calls, retrieval status, memory updates, validation
results, and final response generation. Rather than the frontend inferring any of that from
prose, every node emits one of these typed events through LangGraph's `StreamWriter`
(`get_stream_writer()`, called from inside the node — see `agents/nodes/`), and the SSE endpoint
(`api/v1/chat.py`) forwards each one to the browser essentially unchanged. The panel is a direct
view of the graph's own execution, not a parallel narration a developer could let drift out of
sync with what the graph actually did.

Built in Cycle 3, ahead of `observability/langsmith.py` (Cycle 7) — the two are independent: this
module is what the *browser* sees turn by turn, LangSmith is what a *trace explorer* sees after
the fact. Cycle 7 wires LangSmith tracing on top of this without changing it.
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
