"""Hybrid retrieval: concurrent dense + sparse fan-out, fused by Reciprocal Rank Fusion.

RRF is chosen over a weighted-score blend because dense (cosine-style) and sparse (lexical)
scores live on incompatible scales — a raw 0.53 from the dense index and a raw 12.0 from the
sparse index are not the same "amount of relevance", and picking a blend weight between them
is guesswork that silently degrades quality if the scales ever drift (a model change, a
reranking change). RRF instead uses each hit's *rank* within its own list, which is
scale-free by construction: `score(chunk) = sum(1 / (k + rank))` across every ranked list the
chunk appears in. A chunk that ranks well on both dense and sparse search is rewarded twice;
one that ranks well on only one still surfaces, just lower.

A query fans out over every (namespace, dense|sparse) pair concurrently via `asyncio.gather`,
so an N-namespace query costs one round-trip's worth of wall-clock time, not N sequential
round-trips — see `docs/ARCHITECTURE.md`'s async-engineering criterion.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import structlog

from backend.app.core.errors import VectorStoreUnavailableError
from backend.app.retrieval.models import DEPARTMENTS, RetrievedChunk, allowed_access_levels
from backend.app.retrieval.pinecone_store import IndexKind, PineconeStore

logger = structlog.get_logger(__name__)

# Standard RRF constant (Cormack et al.): large enough that the *rank* dominates the fused
# score rather than the top handful of ranks swamping everything below them.
_RRF_K = 60


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[RetrievedChunk]], *, k: int = _RRF_K
) -> list[RetrievedChunk]:
    """Fuse any number of independently-ranked chunk lists into one, by summed reciprocal
    rank. A chunk's original per-source `score` is not used at all — only its position.

    Ties (equal fused score) preserve each chunk's first-seen order, which in practice means
    "seen earlier in more of its source lists" — a reasonable tiebreak, and a deterministic
    one, which matters for the ingest/retrieval tests and for a demo being reproducible.
    """
    fused_scores: dict[str, float] = {}
    first_seen: dict[str, RetrievedChunk] = {}

    for ranked_list in ranked_lists:
        for rank, chunk in enumerate(ranked_list, start=1):
            fused_scores[chunk.chunk_id] = fused_scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            first_seen.setdefault(chunk.chunk_id, chunk)

    ordered_ids = sorted(fused_scores, key=lambda chunk_id: fused_scores[chunk_id], reverse=True)
    return [
        first_seen[chunk_id].model_copy(update={"score": fused_scores[chunk_id]})
        for chunk_id in ordered_ids
    ]


async def hybrid_search(
    store: PineconeStore,
    *,
    query_text: str,
    role: str,
    namespaces: Sequence[str] | None = None,
    top_k: int = 10,
    candidates_per_source: int = 20,
) -> list[RetrievedChunk]:
    """Search every (namespace, index-kind) pair concurrently and return the top `top_k`
    chunks after RRF fusion, filtered throughout to `role`'s allowed access levels.

    `namespaces=None` searches every department — appropriate when the caller (the Supervisor,
    from Cycle 3) has not narrowed the question to a specific one. A single search-source
    failure degrades (logged, excluded from fusion) rather than failing the whole query; only
    a *total* failure — every source erroring — raises, since at that point there is no
    partial result left to return.
    """
    access_levels = allowed_access_levels(role)
    target_namespaces = list(namespaces) if namespaces else list(DEPARTMENTS)
    index_kinds: tuple[IndexKind, ...] = ("dense", "sparse")

    sources = [(namespace, kind) for namespace in target_namespaces for kind in index_kinds]
    results = await asyncio.gather(
        *(
            store.search(
                index_kind=kind,
                namespace=namespace,
                query_text=query_text,
                top_k=candidates_per_source,
                access_levels=access_levels,
            )
            for namespace, kind in sources
        ),
        return_exceptions=True,
    )

    ranked_lists: list[list[RetrievedChunk]] = []
    failures = 0
    for (namespace, kind), result in zip(sources, results, strict=True):
        if isinstance(result, BaseException):
            failures += 1
            logger.warning(
                "hybrid_search_source_failed",
                namespace=namespace,
                index_kind=kind,
                error=str(result),
            )
            continue
        ranked_lists.append(result)

    if failures == len(sources):
        raise VectorStoreUnavailableError(
            f"All {len(sources)} retrieval sources failed for this query — Pinecone may be unreachable."
        )

    fused = reciprocal_rank_fusion(ranked_lists)
    return fused[:top_k]


def merge_prioritizing_scoped(
    scoped: list[RetrievedChunk], unscoped: list[RetrievedChunk], *, top_k: int
) -> list[RetrievedChunk]:
    """Combine a department-scoped search with an all-department search, keeping every scoped
    hit — it already survived competing only against its own department, so it is never crowded
    out by another department's unrelated top-ranked chunk — and filling any remaining slots
    from the unscoped list, deduped by `chunk_id`, preserving each list's own order. `scoped` is
    empty when no department was identified, in which case this is a no-op that returns
    `unscoped` unchanged (aside from the redundant but harmless re-truncation).

    Shared by `agents/nodes/retrieval.py` (the single-hop retrieval path) and `rlm/api.py`
    (the RLM sandbox's `search` primitive) — both need the identical fix for the identical
    problem: Reciprocal Rank Fusion scores purely by each chunk's rank *within its own list*, so
    several irrelevant departments' locally-top-ranked chunks can collectively outweigh the one
    genuinely relevant department's correct answer once every department is fused together
    (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26). An earlier version of this fix lived only
    in `agents/nodes/retrieval.py`; the RLM path's own unscoped `search()` call was found, live,
    to suffer the identical dilution — see trade-off 26's postscript.

    Never hard-scopes to `scoped` alone: a 4B model's department guess is not reliably grounded,
    and excluding every other department outright made a wrong guess turn a document
    *unreachable* rather than merely diluted — verified live as a real regression before this
    merge-not-replace shape replaced it.
    """
    merged: dict[str, RetrievedChunk] = {chunk.chunk_id: chunk for chunk in scoped}
    for chunk in unscoped:
        if len(merged) >= top_k:
            break
        merged.setdefault(chunk.chunk_id, chunk)
    return list(merged.values())[:top_k]
