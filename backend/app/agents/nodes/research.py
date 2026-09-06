"""Research node: the Supervisor's `"research"` route — the RLM path.

Uses `state["search_query"]` — the Supervisor's context-resolved rewrite of the latest message
(`agents/nodes/supervisor.py`'s module docstring, `docs/ASSUMPTIONS_AND_TRADEOFFS.md`
trade-off 26) — as the research question, falling back to the raw latest message
(`_latest_user_text`) if `search_query` is ever unset. A broad research turn is exactly as
vulnerable to an unresolved follow-up reference as a single-hop retrieval turn is: "and what
about last month?" names no topic on its own, and the generated search plan can only decompose
a question it can actually read.

Delegates everything else to `rlm/executor.py::execute_research`: generating a Python search
plan, validating and running it in `rlm/sandbox.py`, fanning out to recursive sub-agents under
a bounded semaphore, and aggregating. This node's own job is small and mirrors
`agents/nodes/tools.py`'s shape closely —
read the principal, call through to the one place authorization and execution actually happen,
turn the outcome (or any failure) into activity events and state, never let a degradation crash
the turn.

Only ever reached when `agents/nodes/supervisor.py` offered `"research"` as a route at all,
which it only does for a principal holding `Permission.ANALYTICS_TOOLS` — this node does not
re-check that permission itself because there is nothing here to check *against*: unlike
`tools/registry.py`'s named tools, there is no second, independent research-specific handler to
invoke with different authorization, only this one node. The two-layer model
(`docs/DECISIONS.md` §6) is instead expressed as: bind-time filtering lives in the Supervisor's
routing schema, and the execution-boundary equivalent is that `rlm/api.py::build_search` derives
its `access_level` filter from `state["principal_role"]` exactly like `agents/nodes/retrieval.py`
does — so even if a Viewer's turn somehow reached this node, the documents its generated plan
could retrieve would still stop at the Viewer's clearance, never the model's say-so.
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from backend.app.agents.context import GraphContext
from backend.app.agents.principal import principal_from_state
from backend.app.agents.state import AgentState
from backend.app.core.errors import AppError
from backend.app.observability.events import ActivityEvent, ActivityEventType
from backend.app.rlm.executor import execute_research

logger = structlog.get_logger(__name__)


def _latest_user_text(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    raise ValueError("Research node reached with no user message in state.")


def _stringify_research_result(result: Any) -> str:
    """Render whatever the top-level plan's `result` was as text for the Response node.
    Mirrors `rlm/executor.py::_stringify`'s unwrapping of `aggregate`'s dict shape, applied
    here to the *final* result rather than a nested call's — kept as a separate, small
    function instead of importing the private one, since the two are allowed to diverge (this
    one, for instance, could show `recurring_themes` too)."""
    if isinstance(result, dict):
        summary = result.get("summary")
        themes = result.get("recurring_themes")
        if summary is not None:
            if themes:
                return f"{summary}\n\nRecurring themes: {', '.join(str(theme) for theme in themes)}"
            return str(summary)
    return str(result)


async def research_node(state: AgentState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    writer = get_stream_writer()
    writer(
        ActivityEvent(
            event_type=ActivityEventType.NODE_ENTERED,
            node="research",
            message="Generating a research plan.",
        )
    )

    question = state.get("search_query") or _latest_user_text(state["messages"])
    principal = principal_from_state(state)
    settings = runtime.context.settings

    try:
        outcome = await execute_research(
            question=question,
            principal=principal,
            role=state["principal_role"],
            store=runtime.context.pinecone_store,
            llm=runtime.context.llm,
            settings=settings,
        )
    except AppError as exc:
        logger.warning("research_node_degraded", error=str(exc))
        message = f"Research could not be completed: {exc.message}"
        writer(ActivityEvent(event_type=ActivityEventType.ERROR, node="research", message=message))
        return {"research_output": message}

    if outcome.used_fallback_plan:
        writer(
            ActivityEvent(
                event_type=ActivityEventType.ERROR,
                node="research",
                message="The generated search plan was invalid; used the deterministic fallback plan instead.",
            )
        )

    summary = _stringify_research_result(outcome.result)
    writer(
        ActivityEvent(
            event_type=ActivityEventType.RETRIEVAL_STATUS,
            node="research",
            message=f"Research complete ({outcome.sub_agent_calls_made} sub-agent call(s)).",
            data={
                "sub_agent_calls_made": outcome.sub_agent_calls_made,
                "used_fallback_plan": outcome.used_fallback_plan,
            },
        )
    )
    return {"research_output": summary}
