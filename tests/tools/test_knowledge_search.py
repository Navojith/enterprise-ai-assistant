"""Tests for the Knowledge Search tool's wiring — `hybrid_search`/`rerank_chunks` themselves are
covered by `tests/retrieval/`; these confirm the tool spec correctly calls them with the
principal's role (never a role the model could smuggle in) and RBAC-gates the tool itself."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend.app.core.config import get_settings
from backend.app.core.errors import ValidationFailedError
from backend.app.core.security.rbac import Permission, Principal, Role
from backend.app.retrieval.models import RetrievedChunk
from backend.app.tools.knowledge_search import build_knowledge_search_tool
from backend.app.tools.registry import ToolRegistry

_VIEWER = Principal(username="v", role=Role.VIEWER)
_ANALYST = Principal(username="a", role=Role.ANALYST)


def _chunk(chunk_id: str) -> RetrievedChunk:
    return RetrievedChunk.model_validate(
        {
            "chunk_id": chunk_id,
            "document_id": "doc",
            "section": "Summary",
            "text": "text",
            "title": "Title",
            "department": "payments",
            "document_type": "incident",
            "access_level": "internal",
            "created_date": "2026-01-01",
            "score": 1.0,
        }
    )


def _fake_store(chunks: list[RetrievedChunk]) -> AsyncMock:
    store = AsyncMock()
    store.search = AsyncMock(return_value=chunks)
    return store


class TestKnowledgeSearchTool:
    def test_requires_search_permission(self) -> None:
        spec = build_knowledge_search_tool(_fake_store([]), get_settings())

        assert spec.required_permission == Permission.SEARCH

    async def test_returns_chunks_from_hybrid_search(self) -> None:
        store = _fake_store([_chunk("c1")])
        registry = ToolRegistry([build_knowledge_search_tool(store, get_settings())])

        result = await registry.execute(
            "knowledge_search", {"query": "payments outage"}, principal=_ANALYST
        )

        # `score` is not asserted here: it is `hybrid_search`'s own RRF-fused rank, not the raw
        # per-source score this fake store returned — already exhaustively covered by
        # `tests/retrieval/test_hybrid.py`. This test only cares that the tool relays whatever
        # chunks came back, correctly identified.
        assert isinstance(result.data, list)
        assert [chunk["chunk_id"] for chunk in result.data] == ["c1"]

    async def test_no_matches_produces_an_empty_but_successful_result(self) -> None:
        store = _fake_store([])
        registry = ToolRegistry([build_knowledge_search_tool(store, get_settings())])

        result = await registry.execute(
            "knowledge_search", {"query": "nothing matches this"}, principal=_ANALYST
        )

        assert result.data == []
        assert "No matching" in result.summary

    async def test_the_search_uses_the_calling_principals_role_not_a_supplied_one(self) -> None:
        store = _fake_store([])
        registry = ToolRegistry([build_knowledge_search_tool(store, get_settings())])

        await registry.execute("knowledge_search", {"query": "q"}, principal=_VIEWER)

        for call in store.search.await_args_list:
            assert call.kwargs["access_levels"] == ["public", "internal"]

    @pytest.mark.parametrize("bad_top_k", [0, 21])
    async def test_top_k_out_of_range_is_rejected(self, bad_top_k: int) -> None:
        store = _fake_store([])
        registry = ToolRegistry([build_knowledge_search_tool(store, get_settings())])

        with pytest.raises(ValidationFailedError):
            await registry.execute(
                "knowledge_search", {"query": "q", "top_k": bad_top_k}, principal=_ANALYST
            )
