"""Supervisor node: intent classification and routing, plus this turn's memory upkeep.

Routing is schema-constrained (`docs/DECISIONS.md` §5) — the model chooses one of a fixed set of
literal routes, never free text a caller would have to parse back into a decision. Only two
routes exist until later cycles add more: `"retrieval"` for anything that benefits from internal
evidence, `"direct"` for what does not (greetings, clarifying questions). Cycle 4 adds tool
routes; Cycle 5 adds `"research"` for the RLM path.

Memory upkeep — folding old messages into the rolling summary when the thread grows past its
verbatim budget — runs here rather than as a separate graph node, because the Supervisor already
runs first on every turn and reads the full message list to make its routing decision; adding a
dedicated node would mean loading that same state twice. It is still its own distinct
`MEMORY_UPDATE` activity event so the panel shows it as a separate step, matching ASSESSMENT.md's
"memory updates" bullet.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from backend.app.agents.context import GraphContext
from backend.app.agents.state import AgentState
from backend.app.memory.session import build_context_messages
from backend.app.memory.summarizer import needs_summarization, summarize_oldest
from backend.app.observability.events import ActivityEvent, ActivityEventType

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are the routing supervisor for an internal AI assistant at a commercial bank. Decide "
    "whether the user's latest message requires searching internal documents (policies, "
    "incident reports, runbooks, architecture docs, product specs, meeting notes) or can be "
    "answered directly (greetings, clarifying questions, or general questions needing no "
    "company-specific evidence). When genuinely uncertain, prefer retrieval — an evidence-backed "
    "answer is safer than a confident guess."
)


class RoutingDecision(BaseModel):
    """Field order is deliberate and load-bearing, not cosmetic: under JSON-schema-constrained
    (grammar) decoding, the model emits fields in the schema's declared order and commits to
    each one as it is produced. With `reasoning=False` there is no thinking-mode scratch space
    either, so `reasoning` must come *before* `route` in this class — verified live that
    reversing the order (route first) let the model choose `route="direct"` while its own
    `reasoning` field, generated afterward, argued the opposite. Putting reasoning first forces
    the one sentence of deliberation to happen before the choice it is supposed to justify,
    not after.
    """

    reasoning: str = Field(
        description="One sentence of reasoning about what this question needs, written before deciding the route."
    )
    route: Literal["retrieval", "direct"] = Field(
        description="Where to send this turn next, consistent with the reasoning above."
    )


async def supervisor_node(state: AgentState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    writer = get_stream_writer()
    writer(
        ActivityEvent(
            event_type=ActivityEventType.NODE_ENTERED,
            node="supervisor",
            message="Classifying intent and routing.",
        )
    )

    settings = runtime.context.settings
    messages = state["messages"]
    summary = state.get("summary", "")
    recent_messages = messages

    if needs_summarization(
        message_count=len(messages), max_verbatim_messages=settings.memory_max_verbatim_messages
    ):
        remove, summary = await summarize_oldest(
            messages=messages,
            existing_summary=summary,
            batch_size=settings.memory_summarize_batch_size,
            llm=runtime.context.llm,
        )
        recent_messages = messages[settings.memory_summarize_batch_size :]
        writer(
            ActivityEvent(
                event_type=ActivityEventType.MEMORY_UPDATE,
                node="supervisor",
                message=f"Folded {len(remove)} older messages into the rolling summary.",
                data={"summary_length": len(summary)},
            )
        )
    else:
        remove = []

    context_messages = build_context_messages(
        system_prompt=_SYSTEM_PROMPT, summary=summary, recent_messages=recent_messages
    )
    decision = await runtime.context.llm.astructured(
        context_messages, schema=RoutingDecision, reasoning=False
    )
    writer(
        ActivityEvent(
            event_type=ActivityEventType.REASONING,
            node="supervisor",
            message=decision.reasoning,
            data={"route": decision.route},
        )
    )

    updates: dict[str, Any] = {
        "route": decision.route,
        # Reset per-turn validation bookkeeping so a retry loop from a previous turn can never
        # bias this turn's Validator into bailing out early.
        "retry_count": 0,
        "validation_passed": None,
        "validation_feedback": None,
    }
    if remove:
        updates["messages"] = remove
        updates["summary"] = summary
    return updates
