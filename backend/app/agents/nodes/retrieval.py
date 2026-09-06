"""Retrieval node: single-hop RAG over the hybrid dense+sparse index.

Reuses Cycle 1's `hybrid_search` and `rerank_chunks` unchanged — this node's entire job is to
supply them with the right query and role, and turn their result (or failure) into activity
events the panel can show. `docs/DECISIONS.md` §6: the `access_level` filter is derived from
`state["principal_role"]`, set once at graph entry from the verified JWT principal — never from
anything the model has said, so retrieval cannot be widened by a prompt.

A `VectorStoreUnavailableError` degrades to an empty result (`docs/ARCHITECTURE.md`'s
"Failure and degradation" table) rather than failing the turn — the Response node already
handles "no evidence" by saying so explicitly rather than fabricating an answer.

The search query is `state["search_query"]` — the Supervisor's context-resolved rewrite of the
user's latest message (`agents/nodes/supervisor.py`'s module docstring,
`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26) — not the raw message text. A follow-up like
"what are the response steps in that document?" carries almost no retrievable signal on its own;
searching with it verbatim was verified live to return chunks from unrelated departments rather
than the document actually under discussion. `_latest_user_text` remains as the fallback for the
(untested-in-practice) case of `search_query` being unset, so this node degrades to its old
behavior rather than raising if it is ever reached without the Supervisor having run first.

`state["search_department"]`, when the Supervisor could identify one, *prioritizes* that
department rather than replacing the search with it (`_merge_prioritizing_scoped` below) — a
department-scoped search runs alongside the existing all-department one, never instead of it.
An earlier version of this fix passed the guessed department straight through to
`hybrid_search`'s `namespaces` parameter, excluding every other department outright. Verified
live that this was a real regression, not just an incomplete fix: a 4B model's department guess
is not reliably grounded (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26's postscript) — asked
to find the document detailing "certificate rotation", it guessed `core_banking` instead of
`security`, and asked for the "data retention policy" document, it guessed `product` instead of
`human_resources`. Hard-scoping to a wrong guess makes the correct document *unreachable*, which
is strictly worse than the pre-fix behavior this was meant to improve. Running both searches
means a correct guess still gets the intended benefit — protected from cross-department RRF
dilution (trade-off 26, cause 3) — while a wrong guess still has the all-department search as a
full-recall safety net, exactly as before this feature existed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.config import get_stream_writer
from langgraph.runtime import Runtime

from backend.app.agents.context import GraphContext
from backend.app.agents.state import AgentState
from backend.app.core.errors import VectorStoreUnavailableError
from backend.app.observability.events import ActivityEvent, ActivityEventType
from backend.app.retrieval.hybrid import hybrid_search
from backend.app.retrieval.models import RetrievedChunk
from backend.app.retrieval.reranker import rerank_chunks

logger = structlog.get_logger(__name__)

_TOP_K = 8

# The cap on the *merged* result when a department is identified — the sum of both searches'
# own individual budgets (`_TOP_K` each), not `_TOP_K` itself. This was tuned twice, live,
# before landing here, and both wrong turns are worth recording since either alone reads as
# plausible: an equal split (`_TOP_K` scoped + `_TOP_K` unscoped, merged cap `_TOP_K`) starved
# the unscoped safety net completely, because a scoped search into any real department returns
# a full `_TOP_K` on its own — leaving zero merge budget for the all-department fallback to
# contribute anything, silently making a wrong department guess exactly as unrecoverable as the
# hard-scoping this feature was meant to replace. Halving the *scoped* search's own budget
# instead (to `_TOP_K // 2`) broke the opposite, originally-fixed case: the correct chunk for
# the exact reported bug's query only ranked 6th *within its own department's* fused results
# (sparse/BM25 over-rewards several incident "Timeline" sections that happen to mention the
# word "runbook" in passing), so trimming that list to 4 silently dropped it again. A cap equal
# to the sum of both full-sized lists is the only one of the three that drops nothing from
# either list except genuine duplicates — verified live to fix both regressions at once.
_MERGED_TOP_K = _TOP_K * 2


def _latest_user_text(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    raise ValueError("Retrieval node reached with no user message in state.")


def _merge_prioritizing_scoped(
    scoped: list[RetrievedChunk], unscoped: list[RetrievedChunk], *, top_k: int
) -> list[RetrievedChunk]:
    """Combine a department-scoped search with the existing all-department search, keeping
    every scoped hit — it already survived competing only against its own department, so it is
    never crowded out by another department's unrelated top-ranked chunk — and filling any
    remaining slots from the unscoped list, deduped by `chunk_id`, preserving each list's own
    order. `scoped` is empty when no department was identified, in which case this is a no-op
    that returns `unscoped` unchanged (aside from the redundant but harmless re-truncation)."""
    merged: dict[str, RetrievedChunk] = {chunk.chunk_id: chunk for chunk in scoped}
    for chunk in unscoped:
        if len(merged) >= top_k:
            break
        merged.setdefault(chunk.chunk_id, chunk)
    return list(merged.values())[:top_k]


async def retrieval_node(state: AgentState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    writer = get_stream_writer()
    writer(
        ActivityEvent(
            event_type=ActivityEventType.NODE_ENTERED,
            node="retrieval",
            message="Searching internal documents.",
        )
    )

    query = state.get("search_query") or _latest_user_text(state["messages"])
    department = state.get("search_department")
    writer(
        ActivityEvent(
            event_type=ActivityEventType.RETRIEVAL_STATUS,
            node="retrieval",
            message="Querying dense and sparse indexes concurrently.",
            data={"query": query, "department": department or "all"},
        )
    )

    if runtime.context.pinecone_store is None:
        logger.warning("retrieval_node_degraded", reason="pinecone_not_configured")
        writer(
            ActivityEvent(
                event_type=ActivityEventType.ERROR,
                node="retrieval",
                message="Retrieval unavailable (Pinecone not configured), continuing without evidence.",
            )
        )
        return {"retrieved_chunks": []}

    settings = runtime.context.settings
    store = runtime.context.pinecone_store
    role = state["principal_role"]
    try:
        if department:
            # Run the scoped and all-department searches concurrently rather than one after
            # the other — this is one extra Pinecone round-trip per turn, not a sequential
            # doubling of latency. `return_exceptions=True` so one namespace being unreachable
            # doesn't take down the other; only propagate if *both* fail (mirrors
            # `hybrid_search`'s own "a single source degrades, total failure raises" contract).
            scoped_result, unscoped_result = await asyncio.gather(
                hybrid_search(
                    store, query_text=query, role=role, namespaces=[department], top_k=_TOP_K
                ),
                hybrid_search(store, query_text=query, role=role, top_k=_TOP_K),
                return_exceptions=True,
            )
            if isinstance(scoped_result, BaseException) and isinstance(
                unscoped_result, BaseException
            ):
                raise unscoped_result
            scoped = [] if isinstance(scoped_result, BaseException) else scoped_result
            unscoped = [] if isinstance(unscoped_result, BaseException) else unscoped_result
            fused = _merge_prioritizing_scoped(scoped, unscoped, top_k=_MERGED_TOP_K)
        else:
            fused = await hybrid_search(store, query_text=query, role=role, top_k=_TOP_K)
        chunks = await rerank_chunks(store, query=query, chunks=fused, settings=settings)
    except VectorStoreUnavailableError as exc:
        logger.warning("retrieval_node_degraded", error=str(exc))
        writer(
            ActivityEvent(
                event_type=ActivityEventType.ERROR,
                node="retrieval",
                message=f"Retrieval unavailable, continuing without evidence: {exc.message}",
            )
        )
        return {"retrieved_chunks": []}

    writer(
        ActivityEvent(
            event_type=ActivityEventType.RETRIEVAL_STATUS,
            node="retrieval",
            message=f"Found {len(chunks)} relevant chunk(s).",
            data={"chunk_ids": [chunk.chunk_id for chunk in chunks]},
        )
    )
    return {"retrieved_chunks": chunks}
