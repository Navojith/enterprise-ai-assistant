"""Retrieval node: single-hop RAG over the hybrid dense+sparse index.

Reuses Cycle 1's `hybrid_search` and `rerank_chunks` unchanged — this node's entire job is to
supply them with the right query and role, and turn their result (or failure) into activity
events the panel can show. `docs/DECISIONS.md` §6: the `access_level` filter is derived from
`state["principal_role"]`, set once at graph entry from the verified JWT principal — never from
anything the model has said, so retrieval cannot be widened by a prompt.

A `VectorStoreUnavailableError` degrades to an empty result (`docs/ARCHITECTURE.md`'s
"Failure and degradation" table) rather than failing the turn — the Response node already
handles "no evidence" by saying so explicitly rather than fabricating an answer.
"""

from __future__ import annotations

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
from backend.app.retrieval.reranker import rerank_chunks

logger = structlog.get_logger(__name__)

_TOP_K = 8


def _latest_user_text(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    raise ValueError("Retrieval node reached with no user message in state.")


async def retrieval_node(state: AgentState, runtime: Runtime[GraphContext]) -> dict[str, Any]:
    writer = get_stream_writer()
    writer(
        ActivityEvent(
            event_type=ActivityEventType.NODE_ENTERED,
            node="retrieval",
            message="Searching internal documents.",
        )
    )

    query = _latest_user_text(state["messages"])
    writer(
        ActivityEvent(
            event_type=ActivityEventType.RETRIEVAL_STATUS,
            node="retrieval",
            message="Querying dense and sparse indexes concurrently.",
            data={"query": query},
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
    try:
        fused = await hybrid_search(
            runtime.context.pinecone_store,
            query_text=query,
            role=state["principal_role"],
            top_k=_TOP_K,
        )
        chunks = await rerank_chunks(
            runtime.context.pinecone_store, query=query, chunks=fused, settings=settings
        )
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
