"""The Knowledge Search tool: ASSESSMENT.md's "search indexed documents", exposed as a
registry tool rather than only as `agents/nodes/retrieval.py`'s inline call.

Both the Retrieval node's always-on single-hop RAG and this tool call the *same*
`hybrid_search`/`rerank_chunks` pair from Cycle 1 — there is exactly one retrieval
implementation in this codebase, not two that could drift apart. What differs is the caller:
the Retrieval node runs unconditionally on the `"retrieval"` route, while this tool is what the
Supervisor's tool-calling route (`agents/nodes/tools.py`) reaches for when it decides a
targeted, parameterized search is what a turn needs — and, from Cycle 5, what RLM sub-agents
call directly through the `rlm` API.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, Field

from backend.app.core.config import Settings
from backend.app.core.security.rbac import Permission, Principal
from backend.app.retrieval.hybrid import hybrid_search
from backend.app.retrieval.pinecone_store import PineconeStore
from backend.app.retrieval.reranker import rerank_chunks
from backend.app.tools.registry import ToolResult, ToolSpec

TOOL_NAME = "knowledge_search"


class KnowledgeSearchParams(BaseModel):
    """Also the schema the LLM fills in once it has chosen this tool
    (`agents/nodes/tools.py`) — kept small and flat because a 4B model under
    schema-constrained decoding is more reliable the fewer fields it has to commit to
    (`docs/DECISIONS.md` §5)."""

    query: str = Field(description="What to search for in the internal document collection.")
    top_k: int = Field(default=8, ge=1, le=20, description="How many chunks to return.")


def build_knowledge_search_tool(store: PineconeStore, settings: Settings) -> ToolSpec:
    """Factory, not a module-level singleton, because the handler closes over `store` and
    `settings` — both process lifetime singletons built once in `main.py`'s lifespan and
    threaded through `GraphContext`, the same dependency-injection shape every other graph
    node already uses instead of importing a global."""

    async def _handle(principal: Principal, raw_params: BaseModel) -> ToolResult:
        # The registry has already validated `raw_params` against `KnowledgeSearchParams` (this
        # spec's own `params_schema`) before calling the handler — the cast documents that
        # guarantee for mypy rather than re-validating something already checked.
        params = cast(KnowledgeSearchParams, raw_params)
        chunks = await hybrid_search(
            store, query_text=params.query, role=principal.role.value, top_k=params.top_k
        )
        chunks = await rerank_chunks(store, query=params.query, chunks=chunks, settings=settings)
        if not chunks:
            return ToolResult(summary="No matching internal documents were found.", data=[])
        summary = "; ".join(f"[{chunk.title}] {chunk.section}" for chunk in chunks)
        return ToolResult(
            summary=f"Found {len(chunks)} matching chunk(s): {summary}",
            data=[chunk.model_dump(mode="json") for chunk in chunks],
        )

    return ToolSpec(
        name=TOOL_NAME,
        description=(
            "Search the bank's internal document collection (policies, incidents, runbooks, "
            "architecture docs, product specs, meeting notes) for a specific query."
        ),
        required_permission=Permission.SEARCH,
        params_schema=KnowledgeSearchParams,
        handler=_handle,
    )
