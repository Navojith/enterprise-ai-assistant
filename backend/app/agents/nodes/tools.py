"""Tools node: the Supervisor's `"tools"` route, and the one graph node that ever calls
`ToolRegistry.execute` — see `tools/registry.py`'s module docstring for the two-layer
authorization model this node's second LLM call deliberately does not get to shortcut.

Two schema-constrained calls, not one, mirroring how tool-calling works in every major LLM API:
first choose *which* tool from the ones this principal's role actually offers (bind-time
filtering, `docs/DECISIONS.md` §6), then fill in *that* tool's own parameter schema. Splitting
the decision this way keeps each individual schema small — a `Literal` of tool names, then one
tool's own flat parameter model — because a 4B model under grammar-constrained decoding is more
reliable the fewer fields and possibilities it must commit to in a single call
(`docs/DECISIONS.md` §5), the same reasoning that keeps `RoutingDecision` and
`KnowledgeSearchParams` deliberately small.

Every failure mode here — no tools available for this role, the model picks a tool the
execution-boundary re-check still rejects, a tool times out or errors — degrades to a plain
English explanation folded into `tool_output` for the Response node to relay, never a crash and
never a silent gap (`docs/ARCHITECTURE.md`'s "Failure and degradation" table).

`_build_choice_schema`'s `Literal` always includes `_NO_SUITABLE_TOOL` alongside the real tool
names, so the model can say "none of these actually help" instead of being structurally forced
to name one anyway. Added after live testing found the forced choice was a real gap, not a
theoretical one: routed here for a question needing real corpus data it did not have, the
model's own `reasoning` field sometimes concluded, verbatim, that no available tool could help —
and was still made to pick `python_analysis` regardless, which then fabricated data to have
something to compute over (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 30). This is a
structural fix, not a prompting one: even if the routing-level and tool-level prompt guidance
this same investigation added (`agents/nodes/supervisor.py`, `tools/python_analysis.py`) fails
to steer the model away from a bad choice, the schema itself now offers a graceful way out.
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
from backend.app.core.errors import AppError, MCPUnavailableError
from backend.app.memory.session import build_context_messages
from backend.app.observability.events import ActivityEvent, ActivityEventType
from backend.app.tools.registry import ToolSpec

logger = structlog.get_logger(__name__)

_NO_SUITABLE_TOOL = "no_suitable_tool"

_CHOOSE_TOOL_PROMPT = (
    "You are choosing which tool to call to help answer the user's latest message. Pick "
    "exactly one tool from the ones offered below, and give one sentence of reasoning before "
    "your choice. If none of them can actually help — for example, the request needs "
    "information to be searched, retrieved, or looked up first, and no tool here can do "
    f"that on its own — choose {_NO_SUITABLE_TOOL!r} instead of forcing an unsuitable one.\n\n"
    "Available tools:\n{tool_descriptions}"
)

_FILL_ARGS_PROMPT = (
    "You chose the tool {tool_name!r}: {tool_description}\n"
    "Fill in its arguments based on the user's latest message."
)


def _describe_tools(specs: list[ToolSpec]) -> str:
    return "\n".join(f"- {spec.name}: {spec.description}" for spec in specs)


def _build_choice_schema(specs: list[ToolSpec]) -> type[BaseModel]:
    """A fresh `Literal[...]` schema per call, built from whatever tools this principal's role
    currently offers plus `_NO_SUITABLE_TOOL` — never a fixed enum of every tool in the
    registry, so the model is structurally unable to even express choosing a tool bind-time
    filtering already excluded, but always able to decline rather than being forced to name one
    that doesn't actually fit (see the module docstring's note on why this exists)."""
    tool_names = (*(spec.name for spec in specs), _NO_SUITABLE_TOOL)
    return create_model(
        "ToolChoice",
        reasoning=(
            str,
            Field(description="One sentence on which tool best helps, chosen before naming it."),
        ),
        tool_name=(
            Literal[tool_names],
            Field(
                description=(
                    f"The chosen tool's name, or {_NO_SUITABLE_TOOL!r} if none of the "
                    "available tools can actually help with this request."
                )
            ),
        ),
    )


async def tools_node(state: AgentState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    writer = get_stream_writer()
    writer(
        ActivityEvent(
            event_type=ActivityEventType.NODE_ENTERED, node="tools", message="Selecting a tool."
        )
    )

    principal = principal_from_state(state)
    registry = runtime.context.tool_registry
    specs = registry.available_to(principal)

    if not specs:
        message = f"No tools are available to role {principal.role.value!r} for this request."
        writer(ActivityEvent(event_type=ActivityEventType.ERROR, node="tools", message=message))
        return {"tool_output": message}

    llm = runtime.context.llm
    base_messages = state["messages"]

    choice_schema = _build_choice_schema(specs)
    choice_messages = build_context_messages(
        system_prompt=_CHOOSE_TOOL_PROMPT.format(tool_descriptions=_describe_tools(specs)),
        summary=state.get("summary", ""),
        recent_messages=base_messages,
    )
    choice = await llm.astructured(choice_messages, schema=choice_schema, reasoning=False)
    tool_name: str = choice.tool_name  # type: ignore[attr-defined]
    writer(
        ActivityEvent(
            event_type=ActivityEventType.REASONING,
            node="tools",
            message=choice.reasoning,  # type: ignore[attr-defined]
            data={"tool_name": tool_name},
        )
    )

    if tool_name == _NO_SUITABLE_TOOL:
        message = (
            "None of the available tools can fulfil this request — it likely needs internal "
            "document search or a broader research investigation instead of a direct tool call."
        )
        writer(ActivityEvent(event_type=ActivityEventType.ERROR, node="tools", message=message))
        return {"tool_output": message}

    spec = next(spec for spec in specs if spec.name == tool_name)

    args_messages = build_context_messages(
        system_prompt=_FILL_ARGS_PROMPT.format(
            tool_name=spec.name, tool_description=spec.description
        ),
        summary=state.get("summary", ""),
        recent_messages=base_messages,
    )
    arguments = await llm.astructured(args_messages, schema=spec.params_schema, reasoning=False)

    writer(
        ActivityEvent(
            event_type=ActivityEventType.TOOL_CALL,
            node="tools",
            message=f"Calling {spec.name}.",
            data={"tool_name": spec.name, "arguments": arguments.model_dump(mode="json")},
        )
    )
    try:
        result = await registry.execute(
            spec.name, arguments.model_dump(mode="json"), principal=principal
        )
    except MCPUnavailableError as exc:
        logger.warning("tools_node_mcp_unavailable", tool=spec.name, error=str(exc))
        message = f"The {spec.name!r} tool is temporarily unavailable (MCP server unreachable)."
        writer(ActivityEvent(event_type=ActivityEventType.ERROR, node="tools", message=message))
        return {"tool_output": message}
    except AppError as exc:
        logger.warning("tools_node_tool_failed", tool=spec.name, error=str(exc))
        message = f"Tool {spec.name!r} could not be used: {exc.message}"
        writer(ActivityEvent(event_type=ActivityEventType.ERROR, node="tools", message=message))
        return {"tool_output": message}

    writer(
        ActivityEvent(
            event_type=ActivityEventType.TOOL_CALL,
            node="tools",
            message=f"{spec.name} succeeded.",
            data={"tool_name": spec.name, "result": result.summary},
        )
    )
    return {"tool_output": result.summary}
