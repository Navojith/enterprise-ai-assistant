"""Tests for the reranker's allowlist and budget guard — the two cost-guard mechanisms
docs/DELIVERY_PLAN.md's acceptance criteria call out by name.

The Postgres-backed monthly counter (`_increment_monthly_usage`) is monkeypatched rather than
hit for real — these are unit tests of the guard's *logic* (degrade-not-error, allowlist
enforcement), not an integration test of Postgres. The real counter was exercised manually
against a live database; see docs/PROGRESS.md's session log.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.app.core.config import Settings
from backend.app.core.errors import ConfigurationError
from backend.app.retrieval import reranker
from backend.app.retrieval.models import RetrievedChunk


def _chunk(chunk_id: str, text: str = "text") -> RetrievedChunk:
    return RetrievedChunk.model_validate(
        {
            "chunk_id": chunk_id,
            "document_id": "doc",
            "section": "Summary",
            "text": text,
            "title": "T",
            "department": "payments",
            "document_type": "incident",
            "access_level": "internal",
            "created_date": "2026-01-01",
            "score": 0.0,
        }
    )


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


class TestModelAllowlist:
    def test_the_allowed_model_passes(self) -> None:
        reranker._assert_model_allowed("bge-reranker-v2-m3")  # must not raise

    @pytest.mark.parametrize("blocked_model", ["cohere-rerank-3.5", "pinecone-rerank-v0"])
    def test_blocked_models_are_rejected(self, blocked_model: str) -> None:
        with pytest.raises(ConfigurationError, match="billing blocklist"):
            reranker._assert_model_allowed(blocked_model)

    def test_an_unrecognized_model_is_also_rejected(self) -> None:
        """Not just the known-bad models — anything that isn't the one allowed model is
        rejected, so a typo can't accidentally select an unvetted (and possibly billed) model."""
        with pytest.raises(ConfigurationError, match="not the allowlisted model"):
            reranker._assert_model_allowed("some-other-model")


async def test_rerank_disabled_returns_chunks_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    increment = AsyncMock()
    monkeypatch.setattr(reranker, "_increment_monthly_usage", increment)
    store = AsyncMock()
    chunks = [_chunk("a"), _chunk("b")]

    result = await reranker.rerank_chunks(
        store, query="q", chunks=chunks, settings=_settings(rerank_enabled=False)
    )

    assert result == chunks
    increment.assert_not_called()
    store.rerank.assert_not_called()


async def test_rerank_over_budget_degrades_to_original_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reranker, "_increment_monthly_usage", AsyncMock(return_value=451))
    store = AsyncMock()
    chunks = [_chunk("a"), _chunk("b")]

    result = await reranker.rerank_chunks(
        store,
        query="q",
        chunks=chunks,
        settings=_settings(rerank_enabled=True, rerank_monthly_budget=450),
    )

    assert result == chunks
    store.rerank.assert_not_called()


async def test_rerank_call_failure_degrades_to_original_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reranker, "_increment_monthly_usage", AsyncMock(return_value=1))
    store = AsyncMock()
    store.rerank.side_effect = RuntimeError("pinecone rerank endpoint unavailable")
    chunks = [_chunk("a"), _chunk("b")]

    result = await reranker.rerank_chunks(
        store, query="q", chunks=chunks, settings=_settings(rerank_enabled=True)
    )

    assert result == chunks


async def test_rerank_success_reorders_and_rescoring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reranker, "_increment_monthly_usage", AsyncMock(return_value=1))
    store = AsyncMock()
    # The reranker put the second chunk (index 1) first, with a new score.
    store.rerank.return_value = [(1, 0.99), (0, 0.42)]
    chunks = [_chunk("a"), _chunk("b")]

    result = await reranker.rerank_chunks(
        store, query="q", chunks=chunks, settings=_settings(rerank_enabled=True)
    )

    assert [c.chunk_id for c in result] == ["b", "a"]
    assert [c.score for c in result] == [0.99, 0.42]


async def test_rerank_on_empty_input_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    increment = AsyncMock()
    monkeypatch.setattr(reranker, "_increment_monthly_usage", increment)
    store = AsyncMock()

    result = await reranker.rerank_chunks(
        store, query="q", chunks=[], settings=_settings(rerank_enabled=True)
    )

    assert result == []
    increment.assert_not_called()
