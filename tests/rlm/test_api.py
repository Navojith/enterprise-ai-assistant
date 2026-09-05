"""Tests for `rlm/api.py`: the pure helpers directly, and the sync/async bridge through a real
`rlm/sandbox.py::run_sandboxed` call — the bridge is the highest-risk new code this cycle adds
(see `rlm/api.py`'s module docstring), so it is exercised end to end with a real worker thread
and a real event loop rather than mocked away. `hybrid_search` and the LLM are the only two
things faked, matching `tests/llm/test_chain.py`'s "fake the provider, not the plumbing" pattern.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
from collections.abc import AsyncIterator, Sequence
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import BaseMessage, BaseMessageChunk
from pydantic import BaseModel

from backend.app.core.errors import LLMTimeoutError, VectorStoreUnavailableError
from backend.app.core.security.rbac import Principal, Role
from backend.app.llm.provider import SchemaT
from backend.app.retrieval.models import RetrievedChunk
from backend.app.rlm import api
from backend.app.rlm.api import (
    AggregatedFindings,
    RLMBudget,
    RLMContext,
    SubAgentFinding,
    batch_chunks,
    build_rlm_globals,
    build_search,
    build_sub_agent,
    build_sub_agents,
    filter_chunks,
)
from backend.app.rlm.sandbox import run_sandboxed


def _chunk(chunk_id: str = "doc::sec", **overrides: Any) -> RetrievedChunk:
    fields: dict[str, Any] = {
        "chunk_id": chunk_id,
        "document_id": "doc",
        "section": "sec",
        "text": "payment gateway timed out during settlement",
        "title": "Payments Incident Report",
        "department": "payments",
        "document_type": "incident",
        "access_level": "internal",
        "created_date": date(2025, 1, 1).isoformat(),
        "score": 1.0,
    }
    fields.update(overrides)
    return RetrievedChunk.model_validate(fields)


class _FakeLLM:
    """Scripted `LLMProvider`, matching `tests/llm/test_chain.py::_FakeProvider`'s shape
    exactly (including `astream`, unused here but required to structurally satisfy the
    `LLMProvider` Protocol) — trimmed only in that a single result list serves every call."""

    def __init__(
        self, *, results: list[BaseModel] | None = None, error: Exception | None = None
    ) -> None:
        self._results = list(results or [])
        self._error = error
        self.calls = 0

    async def astructured(
        self, messages: Sequence[BaseMessage], *, schema: type[SchemaT], reasoning: bool = False
    ) -> SchemaT:
        self.calls += 1
        if self._error is not None:
            raise self._error
        result = self._results[min(self.calls, len(self._results)) - 1]
        assert isinstance(result, schema)
        return result

    async def astream(
        self, messages: Sequence[BaseMessage], *, reasoning: bool = True
    ) -> AsyncIterator[BaseMessageChunk]:
        raise NotImplementedError("rlm/api.py never calls astream")
        yield  # pragma: no cover - makes this an async generator for typing purposes


def _context(
    *,
    llm: _FakeLLM | None = None,
    max_depth: int = 2,
    max_total_sub_agent_calls: int = 4,
    max_concurrent_sub_agents: int = 4,
    depth: int = 0,
    run_nested_plan: Any = None,
) -> RLMContext:
    async def _default_nested(*, question: str, data: Any, context: RLMContext) -> str:
        raise NotImplementedError("this test does not expect nested-plan recursion")

    return RLMContext(
        loop=asyncio.get_running_loop(),
        captured_vars=contextvars.copy_context(),
        principal=Principal(username="analyst-1", role=Role.ANALYST),
        role="analyst",
        store=AsyncMock(),  # non-None so `search` doesn't short-circuit; `hybrid_search` is faked
        llm=llm or _FakeLLM(),
        budget=RLMBudget(max_depth=max_depth, max_total_sub_agent_calls=max_total_sub_agent_calls),
        depth=depth,
        max_concurrent_sub_agents=max_concurrent_sub_agents,
        run_nested_plan=run_nested_plan or _default_nested,
    )


class TestFilterChunks:
    def test_contains_matches_case_insensitively(self) -> None:
        chunks = [
            _chunk(text="Payment Gateway Timeout"),
            _chunk(chunk_id="other", text="unrelated"),
        ]

        result = filter_chunks(
            [c.model_dump(mode="json") for c in chunks], contains="gateway timeout"
        )

        assert len(result) == 1
        assert result[0]["chunk_id"] == "doc::sec"

    def test_document_type_narrows_the_set(self) -> None:
        chunks = [
            _chunk(chunk_id="a", document_type="incident"),
            _chunk(chunk_id="b", document_type="runbook"),
        ]

        result = filter_chunks([c.model_dump(mode="json") for c in chunks], document_type="runbook")

        assert [c["chunk_id"] for c in result] == ["b"]

    def test_department_narrows_the_set(self) -> None:
        chunks = [
            _chunk(chunk_id="a", department="payments"),
            _chunk(chunk_id="b", department="security"),
        ]

        result = filter_chunks([c.model_dump(mode="json") for c in chunks], department="security")

        assert [c["chunk_id"] for c in result] == ["b"]

    def test_no_criteria_returns_everything_unchanged(self) -> None:
        chunks = [_chunk(chunk_id="a"), _chunk(chunk_id="b")]

        result = filter_chunks([c.model_dump(mode="json") for c in chunks])

        assert len(result) == 2

    def test_combined_criteria_are_all_required(self) -> None:
        chunks = [
            _chunk(chunk_id="a", document_type="incident", department="payments"),
            _chunk(chunk_id="b", document_type="incident", department="security"),
        ]

        result = filter_chunks(
            [c.model_dump(mode="json") for c in chunks],
            document_type="incident",
            department="payments",
        )

        assert [c["chunk_id"] for c in result] == ["a"]


class TestBatchChunks:
    def test_splits_into_fixed_size_groups(self) -> None:
        chunks = [{"i": i} for i in range(7)]

        batches = batch_chunks(chunks, 3)

        assert [len(b) for b in batches] == [3, 3, 1]

    def test_a_non_positive_size_degrades_to_one(self) -> None:
        chunks = [{"i": i} for i in range(3)]

        assert batch_chunks(chunks, 0) == [[{"i": 0}], [{"i": 1}], [{"i": 2}]]
        assert batch_chunks(chunks, -5) == [[{"i": 0}], [{"i": 1}], [{"i": 2}]]

    def test_empty_input_produces_no_batches(self) -> None:
        assert batch_chunks([], 5) == []


class TestRLMBudget:
    def test_reserves_up_to_the_limit_then_refuses(self) -> None:
        budget = RLMBudget(max_depth=2, max_total_sub_agent_calls=2)

        assert budget.try_reserve_sub_agent_call() is True
        assert budget.try_reserve_sub_agent_call() is True
        assert budget.try_reserve_sub_agent_call() is False
        assert budget.sub_agent_calls_made == 2


class TestSyncAsyncBridge:
    """Runs real generated-code-shaped snippets through `rlm/sandbox.py::run_sandboxed` with
    `rlm/api.py`'s functions injected — verifying the worker-thread-to-event-loop bridge itself,
    not just the pure logic each function wraps."""

    async def test_search_bridges_to_hybrid_search_and_returns_chunk_dicts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_hybrid_search(
            store: object, *, query_text: str, role: str, top_k: int
        ) -> list[RetrievedChunk]:
            assert role == "analyst"
            return [_chunk()]

        monkeypatch.setattr(api, "hybrid_search", _fake_hybrid_search)
        context = _context()

        outcome = await run_sandboxed(
            "result = search('payment outages', top_k=5)",
            injected_globals=build_rlm_globals(context),
            timeout_seconds=2.0,
        )

        assert outcome.result == [_chunk().model_dump(mode="json")]

    async def test_search_degrades_to_empty_list_on_vector_store_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _failing_hybrid_search(*args: object, **kwargs: object) -> list[RetrievedChunk]:
            raise VectorStoreUnavailableError("Pinecone unreachable")

        monkeypatch.setattr(api, "hybrid_search", _failing_hybrid_search)
        context = _context()

        outcome = await run_sandboxed(
            "result = search('q')", injected_globals=build_rlm_globals(context), timeout_seconds=2.0
        )

        assert outcome.result == []

    async def test_search_returns_empty_when_store_is_none(self) -> None:
        context = dataclasses.replace(_context(), store=None)

        search = build_search(context)
        result = await asyncio.get_running_loop().run_in_executor(None, search, "q")

        assert result == []

    async def test_sub_agent_bottoms_out_to_a_leaf_finding_at_max_depth(self) -> None:
        llm = _FakeLLM(
            results=[SubAgentFinding(reasoning="because", finding="root cause: timeout")]
        )
        # max_depth=1 with depth=0 means depth+1 (1) >= max_depth (1) — always a leaf call.
        context = _context(llm=llm, max_depth=1)

        outcome = await run_sandboxed(
            "result = sub_agent('what failed?', data)",
            injected_globals={
                **build_rlm_globals(context),
                "data": [_chunk().model_dump(mode="json")],
            },
            timeout_seconds=2.0,
        )

        assert outcome.result == "root cause: timeout"
        assert llm.calls == 1

    async def test_sub_agent_recurses_into_a_nested_plan_below_max_depth(self) -> None:
        calls: list[int] = []

        async def _nested(*, question: str, data: object, context: RLMContext) -> str:
            calls.append(context.depth)
            return "nested finding"

        context = _context(max_depth=2, run_nested_plan=_nested)

        outcome = await run_sandboxed(
            "result = sub_agent('q', data)",
            injected_globals={**build_rlm_globals(context), "data": []},
            timeout_seconds=2.0,
        )

        assert outcome.result == "nested finding"
        assert calls == [1]

    async def test_sub_agent_degrades_once_the_total_budget_is_exhausted(self) -> None:
        llm = _FakeLLM(results=[SubAgentFinding(reasoning="r", finding="f")])
        context = _context(llm=llm, max_depth=1, max_total_sub_agent_calls=0)

        outcome = await run_sandboxed(
            "result = sub_agent('q', data)",
            injected_globals={**build_rlm_globals(context), "data": []},
            timeout_seconds=2.0,
        )

        assert "budget" in str(outcome.result)
        assert llm.calls == 0

    async def test_sub_agents_analyzes_every_batch_concurrently(self) -> None:
        llm = _FakeLLM(
            results=[
                SubAgentFinding(reasoning="r", finding="finding-1"),
                SubAgentFinding(reasoning="r", finding="finding-2"),
            ]
        )
        context = _context(llm=llm, max_depth=1, max_concurrent_sub_agents=2)

        outcome = await run_sandboxed(
            "result = sub_agents('q', data)",
            injected_globals={**build_rlm_globals(context), "data": [[{"i": 1}], [{"i": 2}]]},
            timeout_seconds=2.0,
        )

        assert sorted(outcome.result) == ["finding-1", "finding-2"]
        assert llm.calls == 2

    async def test_aggregate_calls_the_llm_and_returns_a_summary_dict(self) -> None:
        llm = _FakeLLM(
            results=[AggregatedFindings(summary="combined summary", recurring_themes=["timeout"])]
        )
        context = _context(llm=llm)

        outcome = await run_sandboxed(
            "result = aggregate(data, 'what happened?')",
            injected_globals={**build_rlm_globals(context), "data": ["f1", "f2"]},
            timeout_seconds=2.0,
        )

        assert outcome.result == {"summary": "combined summary", "recurring_themes": ["timeout"]}

    async def test_aggregate_degrades_to_joined_findings_on_llm_failure(self) -> None:
        llm = _FakeLLM(error=LLMTimeoutError("timed out"))
        context = _context(llm=llm)

        outcome = await run_sandboxed(
            "result = aggregate(data, 'q')",
            injected_globals={**build_rlm_globals(context), "data": ["f1", "f2"]},
            timeout_seconds=2.0,
        )

        assert outcome.result == {"summary": "f1 f2", "recurring_themes": []}


class TestBuildSubAgentAndSubAgentsDirectly:
    """A thin check that `build_sub_agent`/`build_sub_agents` are wired to the same underlying
    call `TestSyncAsyncBridge` exercises through the sandbox — kept separate so a future
    refactor of one does not accidentally stop covering the other."""

    async def test_build_sub_agent_returns_a_plain_callable(self) -> None:
        llm = _FakeLLM(results=[SubAgentFinding(reasoning="r", finding="ok")])
        context = _context(llm=llm, max_depth=1)
        sub_agent = build_sub_agent(context)

        result = await asyncio.get_running_loop().run_in_executor(None, sub_agent, "q", [])

        assert result == "ok"

    async def test_build_sub_agents_returns_a_plain_callable(self) -> None:
        llm = _FakeLLM(results=[SubAgentFinding(reasoning="r", finding="ok")])
        context = _context(llm=llm, max_depth=1)
        sub_agents = build_sub_agents(context)
        batches: list[list[dict[str, Any]]] = [[]]

        result = await asyncio.get_running_loop().run_in_executor(None, sub_agents, "q", batches)

        assert result == ["ok"]
