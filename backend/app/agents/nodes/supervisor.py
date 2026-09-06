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

The same call also produces `search_query` (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26): a
standalone, context-resolved rewrite of the user's latest message, for `retrieval_node` and
`research_node` to search with instead of the raw message text. This is not a second LLM call —
just one more field on the routing decision the Supervisor already makes with the full message
history in view, which is exactly what a follow-up like "what are the response steps in that
document?" needs to be answered correctly: taken alone, that sentence names no document at all,
so retrieval literally cannot select the right one without conversation context resolving "that
document" first. Live verification found this as a real bug before the fix existed — the raw
follow-up text retrieved chunks from unrelated departments, never the document actually being
discussed — and confirmed fixed afterward.

It also produces `department` (same trade-off 26): the specific department this question is
about, if one is identifiable, so `retrieval_node` can run a search scoped to that one namespace
*alongside* its existing all-department search and prioritize the scoped hits
(`agents/nodes/retrieval.py::_merge_prioritizing_scoped`). Verified live that even a
correctly-identified, correctly-ranked answer within its own department's search results can
still lose to several *other* departments' unrelated top-ranked chunks once Reciprocal Rank
Fusion combines all departments' results — RRF scores purely by each chunk's rank within its
own list, so five irrelevant departments each contributing their own locally-top-ranked (but
globally irrelevant) chunk collectively outweighs the one genuinely relevant department's
correct answer. `"unclear"` is a distinct value, not an empty string, specifically so the model
can express "this genuinely could span departments" under grammar-constrained decoding rather
than being forced to guess one — though guessing one wrong still had to be made *safe*, not just
possible to avoid: hard-scoping to the guessed department (excluding every other one) was this
fix's first version, and was itself found live to be a regression, since a 4B model's department
guess is not reliably grounded and a wrong guess made the correct document unreachable rather
than merely diluted. `retrieval_node` never excludes other departments on the strength of this
field alone; it only prioritizes.

Live testing (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 29) found a further, more subtle
way this guess goes wrong: a question about "payment incidents" got guessed department=`product`
rather than `payments`, because the corpus files an *"Instant Payments Feature Specification"*
under the product department — the model conflated a document's topic with the team that owns
it. `_SYSTEM_PROMPT_TEMPLATE` now says explicitly that keyword overlap with a department's name
is not the same as being about that department. That trade-off's real fix, though, is on the
consuming side: `retrieval/hybrid.py::merge_prioritizing_scoped` no longer lets a wrong guess
crowd out the all-department search's own results, so a guess this prompt tweak still gets
wrong is safe rather than merely less likely.
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
from backend.app.retrieval.models import DEPARTMENTS
from backend.app.tools.registry import ToolSpec

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT_TEMPLATE = (
    "You are the routing supervisor for an internal AI assistant at a commercial bank. Decide "
    "whether the user's latest message requires searching internal documents (policies, "
    "incident reports, runbooks, architecture docs, product specs, meeting notes) via "
    "retrieval, can be answered directly (greetings, clarifying questions, or general "
    "questions needing no company-specific evidence), or is better served by a tool call "
    "({tool_categories}){research_clause}. When genuinely uncertain between retrieval and "
    "direct, prefer retrieval — an evidence-backed answer is safer than a confident guess. You "
    "must also rewrite the user's latest message into a standalone search query: use the "
    'conversation above to resolve any pronoun or reference ("that document", "it", "the '
    'runbook") into the specific document, topic, or keywords being asked about, so the query '
    "makes sense with no other context. If the latest message already stands alone, or the "
    "route does not need a search query, repeat it unchanged. Finally, name the one department "
    "({departments}) this question is about, if it is clearly about one — a question naming a "
    "specific system, document, or team usually is. A question merely containing a word similar "
    "to a department's name is not the same as being about that department — a feature or "
    "incident involving payments could be owned by the product, security, or core banking team "
    "rather than the payments department itself; judge by which team owns the system or process "
    "involved, not by keyword overlap with the department name. Answer 'unclear' if it could "
    "span more than one department or the department cannot be told from the conversation."
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
    `search_query` and `department` come last, after the route is already committed: neither
    needs to precede the routing choice the way `reasoning` does, and by this point the model
    has already articulated what the question is about.
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
        search_query=(
            str,
            Field(
                description=(
                    "The user's latest message, rewritten to stand alone: resolve any pronoun "
                    "or reference using the conversation above (e.g. 'that document' becomes "
                    "the document's actual name). Repeat the message unchanged if it already "
                    "stands alone."
                )
            ),
        ),
        department=(
            Literal[(*DEPARTMENTS, "unclear")],
            Field(
                description=(
                    "The one department this question is about, if clearly identifiable. "
                    "'unclear' if it could span more than one department or cannot be told "
                    "from the conversation."
                )
            ),
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
        departments=", ".join(DEPARTMENTS),
    )
    context_messages = build_context_messages(
        system_prompt=system_prompt, summary=summary, recent_messages=recent_messages
    )
    routing_schema = _build_routing_schema(include_research=research_available)
    decision = await runtime.context.llm.astructured(
        context_messages, schema=routing_schema, reasoning=False
    )
    route: str = decision.route  # type: ignore[attr-defined]
    search_query: str = decision.search_query  # type: ignore[attr-defined]
    department_choice: str = decision.department  # type: ignore[attr-defined]
    search_department = None if department_choice == "unclear" else department_choice
    writer(
        ActivityEvent(
            event_type=ActivityEventType.REASONING,
            node="supervisor",
            message=decision.reasoning,  # type: ignore[attr-defined]
            data={"route": route, "search_query": search_query, "department": department_choice},
        )
    )

    updates: dict[str, Any] = {
        "route": route,
        "search_query": search_query,
        "search_department": search_department,
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
