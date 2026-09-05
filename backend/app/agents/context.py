"""Runtime dependencies threaded through `context=` on every graph invocation.

These are deliberately *not* part of `AgentState`: an `LLMProvider` or `PineconeStore` instance
is not JSON-serializable, and `AsyncPostgresSaver` would fail the first time it tried to persist
one. `state.py` carries conversation data that must survive a checkpoint; this carries
process-local singletons that must not — and are rebuilt fresh (or reused from a cached factory)
on every request instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.core.config import Settings
from backend.app.llm.provider import LLMProvider
from backend.app.retrieval.pinecone_store import PineconeStore


@dataclass
class GraphContext:
    llm: LLMProvider
    # `None` when Pinecone was unreachable or unconfigured at startup — `main.py`'s lifespan
    # degrades to this rather than crashing the process (the same graceful-degradation pattern
    # as a Pinecone outage mid-session), and `retrieval_node` treats it identically to a live
    # `VectorStoreUnavailableError`: continue with no evidence rather than fail the turn.
    pinecone_store: PineconeStore | None
    settings: Settings
