"""Typed graph state, with explicit merge reducers for every field a concurrent writer could
touch.

`AgentState` is a `TypedDict` rather than a dataclass because that is what `langgraph.graph.
StateGraph` checkpoints and diffs natively — a node returns a partial `dict`, and LangGraph
merges it into the running state field by field. A field with no `Annotated[..., reducer]` gets
LangGraph's default "last write wins" merge, which is correct for anything exactly one node
writes per turn (`route`, `draft_answer`, `retry_count`, ...). `messages`, `retrieved_chunks` and
`activity_events` are given explicit reducers instead, because Cycle 5's recursive RLM sub-agents
will write to `retrieved_chunks` from parallel branches in the same superstep — a plain overwrite
there would silently drop one sub-agent's evidence, which is exactly the "butterfly effect"
ASSESSMENT.md's bonus criterion asks a multi-agent design to guard against. Building the reducer
now, before anything actually writes concurrently, means Cycle 5 extends this state instead of
re-deriving it under time pressure.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from backend.app.retrieval.models import RetrievedChunk


def merge_retrieved_chunks(
    existing: list[RetrievedChunk], new: list[RetrievedChunk]
) -> list[RetrievedChunk]:
    """Append `new` chunks to `existing`, deduped by `chunk_id`, keeping whichever instance
    scores higher. The same chunk can legitimately come back from two different searches (the
    single-hop Retrieval node this cycle, and — from Cycle 5 — multiple recursive sub-agents
    each searching a different batch); this reducer is what makes accumulating across those
    writes correct instead of last-write-wins.
    """
    merged: dict[str, RetrievedChunk] = {chunk.chunk_id: chunk for chunk in existing}
    for chunk in new:
        current = merged.get(chunk.chunk_id)
        if current is None or chunk.score > current.score:
            merged[chunk.chunk_id] = chunk
    return list(merged.values())


class AgentState(TypedDict, total=False):
    """The graph's checkpointed state. `total=False` because every node only ever returns the
    subset of fields it actually writes — LangGraph merges the rest in from the prior state."""

    # Full raw turn history for this thread. `add_messages` (LangGraph's standard reducer)
    # appends by default, and also supports removing a message by id — the mechanism
    # `memory/summarizer.py` uses to drop messages it has folded into `summary`.
    messages: Annotated[list[AnyMessage], add_messages]

    # Request-scoped identity, read once at graph entry and carried in state rather than a
    # closure — see docs/DECISIONS.md §6: nodes read authorization from here, never from
    # anything the model emits, and the retrieval node's access-level filter (
    # retrieval/models.py::allowed_access_levels) takes `principal_role` directly.
    principal_username: str
    principal_role: str

    # Rolling-summary memory (memory/summarizer.py): a plain string, replaced wholesale each
    # time it is updated — never accumulated, since each update already folds the prior value in.
    summary: str

    # Supervisor's routing decision. `"direct"` skips retrieval entirely (greetings, questions
    # answerable without evidence); `"retrieval"` is single-hop RAG; `"tools"` (Cycle 4) sends
    # the turn to a specific RBAC-gated tool (knowledge search, Python analysis, or an MCP
    # lookup) instead of the always-on retrieval path — for a request that names a specific
    # lookup (an employee, a service, an incident id) rather than an open-ended question.
    # Cycle 5 adds `"research"` for the RLM path.
    route: Literal["retrieval", "direct", "tools"]

    retrieved_chunks: Annotated[list[RetrievedChunk], merge_retrieved_chunks]

    # The Tools node's result for this turn, folded into the Response node's context exactly
    # like retrieved evidence. `None` when no tool ran (every route but `"tools"`) or when the
    # tool call was denied or failed — in both of those cases this still carries a
    # human-readable explanation, never a silent gap, so the Response node can tell the user
    # what happened rather than fabricating an answer around a missing result.
    tool_output: str | None

    # The Response node's latest draft, re-written on every Validator -> Response retry.
    draft_answer: str

    # Validator's verdict. `None` means "not yet validated this turn" — the initial value the
    # graph's conditional edge checks before the Validator has run.
    validation_passed: bool | None
    validation_feedback: str | None
    retry_count: int
