"""Tests for Reciprocal Rank Fusion and the concurrent hybrid-search orchestration.

`hybrid_search` is tested against a fake store (an `AsyncMock`), not live Pinecone — that keeps
the suite fast and offline; the real integration is exercised manually via `docs/SETUP.md`'s
`python -m scripts.ingest` and confirmed in `docs/PROGRESS.md`'s session log.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.app.core.errors import VectorStoreUnavailableError
from backend.app.retrieval.hybrid import (
    hybrid_search,
    merge_prioritizing_scoped,
    reciprocal_rank_fusion,
)
from backend.app.retrieval.models import RetrievedChunk


def _chunk(chunk_id: str, **overrides: Any) -> RetrievedChunk:
    defaults: dict[str, Any] = {
        "chunk_id": chunk_id,
        "document_id": "doc",
        "section": "Summary",
        "text": "text",
        "title": "T",
        "department": "payments",
        "document_type": "incident",
        "access_level": "internal",
        "created_date": "2026-01-01",
        "score": 0.0,
    }
    defaults.update(overrides)
    return RetrievedChunk.model_validate(defaults)


class TestReciprocalRankFusion:
    def test_chunk_ranked_first_in_both_lists_wins(self) -> None:
        fused = reciprocal_rank_fusion([[_chunk("x"), _chunk("y")], [_chunk("x"), _chunk("z")]])

        assert fused[0].chunk_id == "x"

    def test_fusion_is_scale_free_across_incompatible_source_scores(self) -> None:
        """A dense score of 0.01 and a sparse score of 50.0 must not matter — only rank does,
        which is the entire reason RRF was chosen over a weighted blend (module docstring)."""
        dense = [_chunk("x", score=0.01), _chunk("y", score=0.009)]
        sparse = [_chunk("y", score=50.0), _chunk("x", score=1.0)]

        fused = reciprocal_rank_fusion([dense, sparse])

        # x: rank 1 + rank 2; y: rank 2 + rank 1 — an exact tie, broken by first-seen order.
        assert [c.chunk_id for c in fused] == ["x", "y"]

    def test_chunk_present_in_only_one_list_still_surfaces(self) -> None:
        fused = reciprocal_rank_fusion([[_chunk("only")], []])

        assert [c.chunk_id for c in fused] == ["only"]

    def test_empty_input_produces_empty_output(self) -> None:
        assert reciprocal_rank_fusion([]) == []
        assert reciprocal_rank_fusion([[], []]) == []


async def test_hybrid_search_fuses_dense_and_sparse_across_namespaces() -> None:
    store = AsyncMock()

    async def fake_search(
        *, index_kind: str, namespace: str, query_text: str, top_k: int, access_levels: list[str]
    ) -> list[RetrievedChunk]:
        if namespace == "payments" and index_kind == "dense":
            return [_chunk("p1"), _chunk("p2")]
        if namespace == "payments" and index_kind == "sparse":
            return [_chunk("p1"), _chunk("p3")]
        return []

    store.search.side_effect = fake_search

    results = await hybrid_search(
        store, query_text="q", role="analyst", namespaces=["payments"], top_k=10
    )

    assert results[0].chunk_id == "p1"  # ranked first on both dense and sparse
    assert {c.chunk_id for c in results} == {"p1", "p2", "p3"}


async def test_hybrid_search_degrades_on_partial_source_failure() -> None:
    store = AsyncMock()

    async def fake_search(*, index_kind: str, **_kwargs: Any) -> list[RetrievedChunk]:
        if index_kind == "dense":
            raise RuntimeError("pinecone dense index unreachable")
        return [_chunk("s1")]

    store.search.side_effect = fake_search

    results = await hybrid_search(
        store, query_text="q", role="viewer", namespaces=["payments"], top_k=10
    )

    assert [c.chunk_id for c in results] == ["s1"]


async def test_hybrid_search_raises_when_every_source_fails() -> None:
    store = AsyncMock()
    store.search.side_effect = RuntimeError("pinecone totally unreachable")

    with pytest.raises(VectorStoreUnavailableError):
        await hybrid_search(store, query_text="q", role="viewer", namespaces=["payments"], top_k=10)


class TestMergePrioritizingScoped:
    """See `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26's postscript: a department-scoped
    search must never *replace* the all-department search, only be prioritized within it — a
    wrong department guess must degrade to the same recall as no guess at all, never to zero.
    Shared by `agents/nodes/retrieval.py` and `rlm/api.py::build_search` — both call this
    function directly rather than each keeping their own copy."""

    def test_scoped_hits_are_kept_even_when_unscoped_would_have_ranked_them_lower(self) -> None:
        scoped = [_chunk("a"), _chunk("b")]
        unscoped = [_chunk("c"), _chunk("d")]

        merged = merge_prioritizing_scoped(scoped, unscoped, top_k=8)

        assert [chunk.chunk_id for chunk in merged] == ["a", "b", "c", "d"]

    def test_a_chunk_present_in_both_lists_is_not_duplicated(self) -> None:
        scoped = [_chunk("a")]
        unscoped = [_chunk("a"), _chunk("b")]

        merged = merge_prioritizing_scoped(scoped, unscoped, top_k=8)

        assert [chunk.chunk_id for chunk in merged] == ["a", "b"]

    def test_no_department_identified_falls_back_to_the_unscoped_list_unchanged(self) -> None:
        """The critical safety property: an empty `scoped` list (no department identified, or
        the guessed department's own search came back empty) must never lose recall relative to
        searching every department — it is a pure fallback, not a narrowing."""
        unscoped = [_chunk("a"), _chunk("b"), _chunk("c")]

        merged = merge_prioritizing_scoped([], unscoped, top_k=8)

        assert merged == unscoped

    def test_result_is_capped_at_top_k_even_when_both_lists_are_full(self) -> None:
        scoped = [_chunk(f"s{i}") for i in range(5)]
        unscoped = [_chunk(f"u{i}") for i in range(5)]

        merged = merge_prioritizing_scoped(scoped, unscoped, top_k=6)

        assert len(merged) == 6
        assert [chunk.chunk_id for chunk in merged[:5]] == [f"s{i}" for i in range(5)]
        assert merged[5].chunk_id == "u0"

    def test_a_wrong_department_guess_still_surfaces_the_correct_chunk_from_the_safety_net(
        self,
    ) -> None:
        """The exact regression this function fixes: a scoped search into the *wrong*
        department finds nothing relevant, but the correct chunk is still present because the
        all-department search always runs too."""
        scoped = [_chunk("wrong-dept-chunk", department="core_banking")]
        unscoped = [_chunk("correct-chunk", department="security"), _chunk("wrong-dept-chunk")]

        merged = merge_prioritizing_scoped(scoped, unscoped, top_k=8)

        assert "correct-chunk" in [chunk.chunk_id for chunk in merged]
