r"""LangGraph assembly: wires the Cycle 3 nodes plus Cycle 4's Tools node into one compiled,
checkpointed graph.

```
START -> supervisor --route=retrieval--> retrieval -> response -> validator --pass/exhausted--> END
              |--route=direct-----------------------> response -----^          \--retry--> response
              |--route=tools----------> tools -------> response -----^
              \--route=research-------> research -----> response ----^
```

The `"research"` route (Cycle 5) was additive to this topology, not a rewrite of it — the
conditional-edge function below only needed one more entry in `_SUPERVISOR_ROUTES`, since
`state["route"]`'s schema already widened to include it (`agents/nodes/supervisor.py`).

The checkpointer is accepted as a parameter, built by `main.py`'s lifespan and owned by it for
the process's lifetime (`AsyncPostgresSaver` needs an open connection/pool and its own `setup()`
call — see `docs/ASSUMPTIONS_AND_TRADEOFFS.md` assumption 6 on why this is a second, independent
Postgres touchpoint from `core/db.py`'s SQLAlchemy engine, not a shared one), rather than this
module constructing one itself — this keeps `agents/graph.py` a pure "what does the graph look
like" module, testable by compiling it against an in-memory checkpointer with no real database.
"""

from __future__ import annotations

from collections.abc import Callable

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.app.agents.context import GraphContext
from backend.app.agents.nodes.research import research_node
from backend.app.agents.nodes.response import response_node
from backend.app.agents.nodes.retrieval import retrieval_node
from backend.app.agents.nodes.supervisor import supervisor_node
from backend.app.agents.nodes.tools import tools_node
from backend.app.agents.nodes.validator import validator_node
from backend.app.agents.state import AgentState

_SUPERVISOR_ROUTES = {"retrieval": "retrieval", "tools": "tools", "research": "research"}


def _after_supervisor(state: AgentState) -> str:
    return _SUPERVISOR_ROUTES.get(state.get("route", ""), "response")


def _make_after_validator(*, max_validator_retries: int) -> Callable[[AgentState], str]:
    """Closes over `max_validator_retries` at graph-build time rather than reading it from
    `Runtime` at route time — this setting is fixed for the process's lifetime, so there is no
    need for a per-request lookup, and a conditional-edge function that takes only `state` (the
    same signature `_after_supervisor` uses) is directly unit-testable with no `Runtime`/context
    to fake. `validator_node` reads the identical setting from its own `Runtime[GraphContext]`
    parameter, so the two can never disagree about where the loop ends — they are both reading
    `Settings.max_validator_retries`, just via different, equally valid access paths.
    """

    def _after_validator(state: AgentState) -> str:
        if state.get("validation_passed"):
            return END
        if state.get("retry_count", 0) > max_validator_retries:
            return END
        return "response"

    return _after_validator


def build_graph(
    checkpointer: BaseCheckpointSaver[str], *, max_validator_retries: int
) -> CompiledStateGraph[AgentState, GraphContext]:
    graph: StateGraph[AgentState, GraphContext] = StateGraph(
        AgentState, context_schema=GraphContext
    )

    graph.add_node("supervisor", supervisor_node)
    graph.add_node("retrieval", retrieval_node)
    graph.add_node("tools", tools_node)
    graph.add_node("research", research_node)
    graph.add_node("response", response_node)
    graph.add_node("validator", validator_node)

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _after_supervisor,
        {
            "retrieval": "retrieval",
            "tools": "tools",
            "research": "research",
            "response": "response",
        },
    )
    graph.add_edge("retrieval", "response")
    graph.add_edge("tools", "response")
    graph.add_edge("research", "response")
    graph.add_edge("response", "validator")
    after_validator = _make_after_validator(max_validator_retries=max_validator_retries)
    graph.add_conditional_edges("validator", after_validator, {"response": "response", END: END})

    return graph.compile(checkpointer=checkpointer)
