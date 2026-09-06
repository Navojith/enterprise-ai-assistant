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
    # `"research"` (Cycle 5) is the RLM path: a generated Python search plan, decomposing a
    # broad question into batches analyzed by recursive sub-agents and aggregated — offered to
    # the Supervisor only for principals holding `Permission.ANALYTICS_TOOLS`, since it runs on
    # the same `rlm/sandbox.py` execution boundary `python_analysis` does
    # (`agents/nodes/supervisor.py::_available_routes`).
    route: Literal["retrieval", "direct", "tools", "research"]

    # A standalone, context-resolved version of the user's request, written by the Supervisor
    # (`agents/nodes/supervisor.py::_build_routing_schema`) in the same schema-constrained call
    # that decides `route` — not a second LLM call, just one more field on the same decision.
    # `retrieval_node` and `research_node` search with this instead of the raw latest message,
    # because a follow-up turn's literal text ("what are the response steps in that document?")
    # carries almost no lexical or semantic signal about *which* document once it is taken out
    # of the conversation it depends on; the Supervisor sees the full message history and can
    # resolve "that document" into the actual document name before search ever runs. Verified
    # live that this was a real, reproducible bug (`docs/ASSUMPTIONS_AND_TRADEOFFS.md`
    # trade-off 26): the raw-message query retrieved zero chunks from the correct document on
    # two consecutive follow-up turns.
    search_query: str

    # The one department (`retrieval/models.py::DEPARTMENTS`) this turn's question is about, if
    # the Supervisor could tell — `None` when it genuinely could span departments or isn't
    # identifiable. `retrieval_node` runs a search scoped to this department *alongside* its
    # existing all-department search and prioritizes the scoped hits
    # (`agents/nodes/retrieval.py::_merge_prioritizing_scoped`) rather than replacing the search
    # with it. Verified live that a department-scoped chunk needs this protection even when it
    # is correctly the top-ranked result *within its own department*: fanning out across every
    # department means Reciprocal Rank Fusion also ranks each *other* department's own
    # top-ranked (but topically irrelevant) chunk highly, since RRF scores purely by a chunk's
    # rank within its own list — several irrelevant departments each contributing one
    # high-ranked chunk can collectively outweigh the one relevant department's genuinely
    # correct answer. Excluding other departments outright was the first version of this fix,
    # and was itself a real, live-verified regression: a 4B model's department guess is not
    # reliably grounded (it guessed `core_banking` for a "certificate rotation" question that
    # was actually `security`), and hard-scoping to a wrong guess makes the correct document
    # unreachable rather than merely diluted (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26's
    # postscript).
    search_department: str | None

    retrieved_chunks: Annotated[list[RetrievedChunk], merge_retrieved_chunks]

    # The Tools node's result for this turn, folded into the Response node's context exactly
    # like retrieved evidence. `None` when no tool ran (every route but `"tools"`) or when the
    # tool call was denied or failed — in both of those cases this still carries a
    # human-readable explanation, never a silent gap, so the Response node can tell the user
    # what happened rather than fabricating an answer around a missing result.
    tool_output: str | None

    # The Research node's (Cycle 5) result for this turn — the RLM executor's aggregated
    # summary, or a plain-English explanation if research degraded (Pinecone or the LLM
    # unreachable). `None` on every route but `"research"`. Folded into the Response node's
    # context alongside `tool_output` rather than replacing it, since a future turn could in
    # principle route through both in sequence via the checkpointed conversation history.
    research_output: str | None

    # The Response node's latest draft, re-written on every Validator -> Response retry.
    draft_answer: str

    # Validator's verdict. `None` means "not yet validated this turn" — the initial value the
    # graph's conditional edge checks before the Validator has run.
    validation_passed: bool | None
    validation_feedback: str | None
    retry_count: int
