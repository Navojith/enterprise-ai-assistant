"""Validator node: verifies the Response node's draft against real evidence and brand rules, and
is the exit condition for the bounded Validator -> Response retry loop.

Cycle 3 shipped a structural-only check (non-empty, cited-if-evidenced). Cycle 6 replaces that
with `_validate_answer`, layering two real guardrail checks on top of the same structural ones:

- **Citation verification** (`guardrails/citations.py`) — does every bracketed `[Title]` marker
  in the draft correspond to a chunk this turn actually retrieved, or a real tool/research
  result, rather than a title the model fabricated to look well-sourced?
- **Brand/persona check** (`guardrails/brand.py`) — does the draft stay in character as this
  bank's own assistant, and does it avoid echoing the system prompt back to the user?

The node's shape is unchanged from Cycle 3: a pure check feeding a pass/fail decision, feeding
the same bounded retry loop, with feedback fed back into the Response node's next attempt
(`response.py::_build_system_prompt`'s `validation_feedback` branch).

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
from backend.app.agents.nodes.response import SYSTEM_PROMPT
from backend.app.agents.state import AgentState
from backend.app.guardrails.brand import check_brand_violation
from backend.app.guardrails.citations import verify_citations
from backend.app.observability.events import ActivityEvent, ActivityEventType
from backend.app.retrieval.models import RetrievedChunk

logger = structlog.get_logger(__name__)

_UNVALIDATED_CAVEAT = "\n\n*(This answer could not be fully validated — treat it with extra care.)*"


def _validate_answer(
    *,
    answer: str,
    retrieved_chunks: list[RetrievedChunk],
    tool_output: str | None,
    research_output: str | None,
) -> str | None:
    """Returns `None` if `answer` passes every check, else feedback explaining the first one it
    failed. Pure and synchronous — unit-testable with no graph, LLM, or state involved.

    Checks run cheapest/most-certain first, so the Response node's revision prompt always names
    one concrete, actionable problem rather than the last of several unrelated ones: empty ->
    missing citation entirely -> a citation naming something that was never retrieved ->
    breaking persona or leaking the system prompt.
    """
    if not answer.strip():
        return "The answer was empty."

    had_evidence = bool(retrieved_chunks)
    if had_evidence and "[" not in answer:
        return "Evidence was retrieved but the answer includes no bracketed citation."

    hallucinated = verify_citations(
        answer=answer,
        retrieved_chunks=retrieved_chunks,
        tool_output=tool_output,
        research_output=research_output,
    )
    if hallucinated:
        return (
            f"The answer cites {hallucinated!r}, which does not match any document, tool "
            "result, or research finding actually retrieved this turn."
        )

    return check_brand_violation(answer, SYSTEM_PROMPT)


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
    feedback = _validate_answer(
        answer=answer,
        retrieved_chunks=state.get("retrieved_chunks", []),
        tool_output=state.get("tool_output"),
        research_output=state.get("research_output"),
    )
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
