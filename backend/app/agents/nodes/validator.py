"""Validator node: this cycle's structural check on the Response node's draft, and the exit
condition for the bounded Validator -> Response retry loop.

Only a structural check today — non-empty, and cited if evidence was retrieved. Cycle 6 replaces
`_validate_structurally` with real citation verification against retrieved chunk ids and the
brand/injection guardrails (`docs/ARCHITECTURE.md`'s Security and guardrails criterion); this
node's shape — a pure check feeding a pass/fail decision — does not change, only what the check
looks at.

The retry loop is bounded by `settings.max_validator_retries`
(`docs/ARCHITECTURE.md`'s "bounded validator->response retry loop"): once exhausted, this node
stops looping and appends the last draft to the permanent conversation history anyway, with a
caveat, rather than either discarding the user's answer or looping forever — graceful
degradation, not silent failure.
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.messages import AIMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from backend.app.agents.context import GraphContext
from backend.app.agents.state import AgentState
from backend.app.observability.events import ActivityEvent, ActivityEventType

logger = structlog.get_logger(__name__)

_UNVALIDATED_CAVEAT = "\n\n*(This answer could not be fully validated — treat it with extra care.)*"


def _validate_structurally(*, answer: str, had_evidence: bool) -> str | None:
    """Returns `None` if `answer` passes, else a feedback string explaining why it did not.
    Pure and synchronous — unit-testable with no graph, LLM, or state involved."""
    if not answer.strip():
        return "The answer was empty."
    if had_evidence and "[" not in answer:
        return "Evidence was retrieved but the answer includes no bracketed citation."
    return None


async def validator_node(state: AgentState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    writer = get_stream_writer()
    writer(
        ActivityEvent(
            event_type=ActivityEventType.NODE_ENTERED,
            node="validator",
            message="Validating the draft answer.",
        )
    )

    answer = state.get("draft_answer", "")
    had_evidence = bool(state.get("retrieved_chunks"))
    feedback = _validate_structurally(answer=answer, had_evidence=had_evidence)
    passed = feedback is None

    writer(
        ActivityEvent(
            event_type=ActivityEventType.VALIDATION_RESULT,
            node="validator",
            message="Passed." if passed else f"Failed: {feedback}",
            data={"passed": passed},
        )
    )

    if passed:
        return {
            "validation_passed": True,
            "validation_feedback": None,
            "messages": [AIMessage(content=answer)],
        }

    retry_count = state.get("retry_count", 0) + 1
    max_retries = runtime.context.settings.max_validator_retries
    if retry_count > max_retries:
        writer(
            ActivityEvent(
                event_type=ActivityEventType.VALIDATION_RESULT,
                node="validator",
                message=f"Retry budget ({max_retries}) exhausted — returning the last draft with a caveat.",
            )
        )
        return {
            "validation_passed": False,
            "validation_feedback": feedback,
            "retry_count": retry_count,
            "messages": [AIMessage(content=answer + _UNVALIDATED_CAVEAT)],
        }

    return {"validation_passed": False, "validation_feedback": feedback, "retry_count": retry_count}
