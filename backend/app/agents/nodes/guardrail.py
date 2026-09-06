"""Guardrail node: the graph's first node, screening every user message for prompt injection
before anything else runs.

`docs/DELIVERY_PLAN.md`'s own acceptance criterion 5 requires an injection attempt to be
"blocked and traced" — which is why this is a graph node, not a check at the API boundary before
`graph.astream()` is even called: a check outside the graph would never appear in a LangSmith
trace or the Agent Activity Panel (`observability/events.py`'s module docstring: the panel is
the graph's own execution, not a parallel narration), and the assessment explicitly wants the
evaluator able to *observe* this decision, not just receive an HTTP error for it.

A block is a raised `GuardrailViolationError`, not a state field the graph routes around,
because `api/v1/chat.py::_stream_turn` already has exactly the right handling for it: any
`AppError` raised mid-stream becomes one more SSE event rather than crashing the connection.
Reusing that path means this node needs no new plumbing on the API side — it is caught by code
that already existed for a different failure mode (a `GraphUnavailableError`, an `LLMError`, ...).

See `guardrails/injection.py`'s module docstring for the two-tier design (deterministic
heuristics, escalating to a schema-constrained classifier only when the heuristics are
genuinely unsure) this node executes.
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from backend.app.agents.context import GraphContext
from backend.app.agents.state import AgentState
from backend.app.core.errors import AppError, GuardrailViolationError
from backend.app.guardrails.injection import (
    CLASSIFIER_SYSTEM_PROMPT,
    InjectionClassification,
    InjectionVerdict,
    heuristic_screen,
)
from backend.app.observability.events import ActivityEvent, ActivityEventType

logger = structlog.get_logger(__name__)


def _latest_user_text(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    raise ValueError("Guardrail node reached with no user message in state.")


async def guardrail_node(state: AgentState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    writer = get_stream_writer()
    writer(
        ActivityEvent(
            event_type=ActivityEventType.NODE_ENTERED,
            node="guardrail",
            message="Screening the message for prompt injection.",
        )
    )

    text = _latest_user_text(state["messages"])
    screen = heuristic_screen(text)

    if screen.verdict is InjectionVerdict.BLOCK:
        writer(
            ActivityEvent(
                event_type=ActivityEventType.VALIDATION_RESULT,
                node="guardrail",
                message=f"Blocked: {screen.reason}",
                data={"passed": False, "category": screen.category},
            )
        )
        raise GuardrailViolationError(
            "This request was blocked by the prompt-injection guardrail.",
            details={"category": screen.category, "reason": screen.reason},
        )

    if screen.verdict is InjectionVerdict.ALLOW:
        writer(
            ActivityEvent(
                event_type=ActivityEventType.VALIDATION_RESULT,
                node="guardrail",
                message="Passed the heuristic injection screen.",
                data={"passed": True},
            )
        )
        return {}

    # AMBIGUOUS: escalate to one schema-constrained classifier call rather than guessing either
    # way — see `guardrails/injection.py`'s module docstring for why this tier exists at all.
    writer(
        ActivityEvent(
            event_type=ActivityEventType.REASONING,
            node="guardrail",
            message=f"Heuristic screen was inconclusive ({screen.reason}); asking the classifier.",
        )
    )
    try:
        classification = await runtime.context.llm.astructured(
            [SystemMessage(content=CLASSIFIER_SYSTEM_PROMPT), HumanMessage(content=text)],
            schema=InjectionClassification,
            reasoning=False,
        )
    except AppError as exc:
        # Fails open, deliberately: the heuristic screen above already ruled out every confident
        # attack pattern, so the residual risk here is a paraphrased attack the classifier might
        # have caught — not a known one. This codebase treats every other non-authorization
        # dependency (Pinecone, MCP, the rerank budget) the same way, degrading rather than
        # blocking the user on an infrastructure hiccup; recorded as a deliberate trade-off in
        # docs/ASSUMPTIONS_AND_TRADEOFFS.md, not an oversight.
        logger.warning("guardrail_classifier_unavailable", error=str(exc))
        writer(
            ActivityEvent(
                event_type=ActivityEventType.VALIDATION_RESULT,
                node="guardrail",
                message=f"Classifier unavailable ({exc.message}); failing open.",
                data={"passed": True, "degraded": True},
            )
        )
        return {}

    verdict = classification.verdict
    reasoning = classification.reasoning
    if verdict == "unsafe":
        writer(
            ActivityEvent(
                event_type=ActivityEventType.VALIDATION_RESULT,
                node="guardrail",
                message=f"Blocked by classifier: {reasoning}",
                data={"passed": False, "category": "classifier"},
            )
        )
        raise GuardrailViolationError(
            "This request was blocked by the prompt-injection guardrail.",
            details={"category": "classifier", "reasoning": reasoning},
        )

    writer(
        ActivityEvent(
            event_type=ActivityEventType.VALIDATION_RESULT,
            node="guardrail",
            message="Classifier verdict: safe.",
            data={"passed": True},
        )
    )
    return {}
