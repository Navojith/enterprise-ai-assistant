"""Supervisor node: intent classification and routing, plus this turn's memory upkeep.

Routing is schema-constrained (`docs/DECISIONS.md` §5) — the model chooses one of a fixed set of
literal routes, never free text a caller would have to parse back into a decision. `"retrieval"`
is for anything that benefits from internal document evidence, `"direct"` for what does not
(greetings, clarifying questions), `"tools"` (Cycle 4) for a request naming a specific lookup a
tool answers directly — an employee, a service, an incident id, or a small computation over data
already in the conversation — rather than an open-ended question, and `"research"` (Cycle 5) for
a broad, multi-document investigation better served by a generated Python search plan than a
single retrieval pass (`docs/ARCHITECTURE.md`'s RLM example: summarizing a year of incidents and
identifying recurring root causes).

The prompt only ever names the tool *categories* this principal's role actually has
(`_available_tool_categories`, from `runtime.context.tool_registry.available_to`) — a Viewer,
who has no tools beyond search, is never told "tools" exist for anything beyond that, so it
never routes there for an analytics or MCP-shaped request in the first place. This is bind-time
filtering applied one level up from `tools/registry.py`'s own (which narrows what
`agents/nodes/tools.py` later offers *as a specific tool name*): the Supervisor is filtered on
tool *categories*, the Tools node on tool *names* — both read from the same `ToolRegistry`, so
neither can drift from what `tools/registry.py::execute` will actually allow at the boundary.
`"research"` gets the identical treatment via `_build_routing_schema`: it is only ever a candidate
value in the routing schema for a principal holding `Permission.ANALYTICS_TOOLS`, because
`agents/nodes/research.py` executes generated code on the same `rlm/sandbox.py` execution
boundary `python_analysis` does — a Viewer's equivalent question still gets an answer, just
via `"retrieval"`'s single-hop path instead of a multi-batch investigation.

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
from pydantic import BaseModel, Field, create_model

from backend.app.agents.context import GraphContext
from backend.app.agents.principal import principal_from_state
from backend.app.agents.state import AgentState
from backend.app.core.security.rbac import Permission
from backend.app.memory.session import build_context_messages
from backend.app.memory.summarizer import needs_summarization, summarize_oldest
from backend.app.observability.events import ActivityEvent, ActivityEventType
from backend.app.tools.registry import ToolSpec

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT_TEMPLATE = (
    "You are the routing supervisor for an internal AI assistant at a commercial bank. Decide "
    "whether the user's latest message requires searching internal documents (policies, "
    "incident reports, runbooks, architecture docs, product specs, meeting notes) via "
    "retrieval, can be answered directly (greetings, clarifying questions, or general "
    "questions needing no company-specific evidence), or is better served by a tool call "
    "({tool_categories}){research_clause}. When genuinely uncertain between retrieval and "
    "direct, prefer retrieval — an evidence-backed answer is safer than a confident guess."
)

# Only ever appended when this principal holds `Permission.ANALYTICS_TOOLS` — see the module
# docstring's note on `research` getting the same bind-time treatment as `tools`.
_RESEARCH_CLAUSE = (
    ", or, for a broad investigation spanning many documents that benefits from being "
    "decomposed into batches and analyzed separately (e.g. summarizing a year of incidents "
    "and identifying recurring root causes), route to research"
)

# Human-readable gloss for each `Permission` a tool can require, used only to describe *what
# kinds* of tools exist in the routing prompt — never to decide access, which is entirely
# `ToolRegistry.available_to`'s and `tools/registry.py::execute`'s job.
_TOOL_CATEGORY_BY_PERMISSION: dict[Permission, str] = {
    Permission.SEARCH: "a dedicated internal-document search",
    Permission.ANALYTICS_TOOLS: "a small Python analysis over data already in the conversation",
    Permission.MCP_TOOLS: "looking up an employee, a service, or an incident record by id or name",
}


def _available_tool_categories(specs: list[ToolSpec]) -> str:
    """De-duplicated, role-filtered tool categories for the routing prompt — e.g. an Analyst
    sees all three, a Viewer sees none and the `"tools"` route effectively never applies to
    them, without the prompt needing an explicit role branch to say so."""
    permissions_offered = {spec.required_permission for spec in specs}
    categories = [
        text
        for permission, text in _TOOL_CATEGORY_BY_PERMISSION.items()
        if permission in permissions_offered
    ]
    return "; ".join(categories) if categories else "no tools are available for this request"


_ROUTES_WITHOUT_RESEARCH = ("retrieval", "direct", "tools")
_ROUTES_WITH_RESEARCH = (*_ROUTES_WITHOUT_RESEARCH, "research")


def _build_routing_schema(*, include_research: bool) -> type[BaseModel]:
    """A fresh `Literal[...]` schema per call, matching `agents/nodes/tools.py::
    _build_choice_schema`'s reasoning exactly: `"research"` is only ever a value this schema
    can even express when `include_research` is `True`, which `supervisor_node` decides from
    `principal.has_permission(Permission.ANALYTICS_TOOLS)` — a Viewer's routing decision is
    structurally unable to choose `"research"`, the same bind-time filtering
    `tools/registry.py` applies to specific tool names, applied here to a route.

    Field order is deliberate and load-bearing, not cosmetic: under JSON-schema-constrained
    (grammar) decoding, the model emits fields in the schema's declared order and commits to
    each one as it is produced. With `reasoning=False` there is no thinking-mode scratch space
    either, so `reasoning` must come *before* `route` — verified live that reversing the order
    (route first) let the model choose `route="direct"` while its own `reasoning` field,
    generated afterward, argued the opposite. Putting reasoning first forces the one sentence
    of deliberation to happen before the choice it is supposed to justify, not after.
    """
    routes = _ROUTES_WITH_RESEARCH if include_research else _ROUTES_WITHOUT_RESEARCH
    return create_model(
        "RoutingDecision",
        reasoning=(
            str,
            Field(
                description="One sentence of reasoning about what this question needs, written before deciding the route."
            ),
        ),
        route=(
            Literal[routes],
            Field(description="Where to send this turn next, consistent with the reasoning above."),
        ),
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

    principal = principal_from_state(state)
    available_specs = runtime.context.tool_registry.available_to(principal)
    research_available = principal.has_permission(Permission.ANALYTICS_TOOLS)
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        tool_categories=_available_tool_categories(available_specs),
        research_clause=_RESEARCH_CLAUSE if research_available else "",
    )
    context_messages = build_context_messages(
        system_prompt=system_prompt, summary=summary, recent_messages=recent_messages
    )
    routing_schema = _build_routing_schema(include_research=research_available)
    decision = await runtime.context.llm.astructured(
        context_messages, schema=routing_schema, reasoning=False
    )
    route: str = decision.route  # type: ignore[attr-defined]
    writer(
        ActivityEvent(
            event_type=ActivityEventType.REASONING,
            node="supervisor",
            message=decision.reasoning,  # type: ignore[attr-defined]
            data={"route": route},
        )
    )

    updates: dict[str, Any] = {
        "route": route,
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
