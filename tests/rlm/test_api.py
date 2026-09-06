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
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import BaseMessage, BaseMessageChunk
from pydantic import BaseModel

from backend.app.core.config import Settings
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
    group_by_document,
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
        self,
        *,
        results: list[BaseModel] | None = None,
        error: Exception | None = None,
        fail_on_call: int | None = None,
    ) -> None:
        self._results = list(results or [])
        self._error = error
        # `fail_on_call` (1-indexed) raises `error` only on that specific call, falling through
        # to `_results` on every other call — needed to test a retry's *own* call failing
        # without also breaking every existing test that passes `error=` alone expecting it to
        # fail unconditionally (the default, `fail_on_call=None`, preserves that exact behavior).
        self._fail_on_call = fail_on_call
        self.calls = 0
        # Recorded per call so a test can assert *what* was requested, not just how many times —
        # e.g. that `build_aggregate` actually asked for a lower, more deterministic temperature.
        self.temperatures_requested: list[float | None] = []

    async def astructured(
        self,
        messages: Sequence[BaseMessage],
        *,
        schema: type[SchemaT],
        reasoning: bool = False,
        temperature: float | None = None,
    ) -> SchemaT:
        self.calls += 1
        self.temperatures_requested.append(temperature)
        if self._error is not None and self._fail_on_call in (None, self.calls):
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
    department: str | None = None,
    settings: Settings | None = None,
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
        # Reranking disabled by default so `search`-exercising tests never touch Postgres for
        # the monthly-usage counter — `TestSearchReranking` below turns it on explicitly against
        # a faked `rerank_chunks` instead of a real one.
        settings=settings or Settings(_env_file=None, rerank_enabled=False),
        budget=RLMBudget(max_depth=max_depth, max_total_sub_agent_calls=max_total_sub_agent_calls),
        depth=depth,
        max_concurrent_sub_agents=max_concurrent_sub_agents,
        run_nested_plan=run_nested_plan or _default_nested,
        department=department,
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

    def test_an_unrecognized_document_type_is_ignored_rather_than_matching_nothing(self) -> None:
        """The exact live-verified failure: a generated plan filtered on `document_type="outage
        report"` — not a real value — and got an empty result with no error anywhere to explain
        why. An invented value must degrade to "no filter", never to "everything excluded"."""
        chunks = [_chunk(chunk_id="a", document_type="incident")]

        result = filter_chunks(
            [c.model_dump(mode="json") for c in chunks], document_type="outage report"
        )

        assert [c["chunk_id"] for c in result] == ["a"]

    def test_an_unrecognized_department_is_ignored_rather_than_matching_nothing(self) -> None:
        chunks = [_chunk(chunk_id="a", department="payments")]

        result = filter_chunks(
            [c.model_dump(mode="json") for c in chunks], department="not-a-real-department"
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


class TestGroupByDocument:
    def test_never_splits_one_document_across_two_batches(self) -> None:
        """The exact live-verified bug `batch()` caused: a document's sections (e.g. an
        incident's Root Cause and Summary) landing in different batches, so no sub-agent ever
        sees the whole document together. A small `max_batch_size` forces multiple batches;
        each document's chunks must all land in exactly one of them."""
        chunks = [
            {"document_id": "doc-a", "section": "Summary"},
            {"document_id": "doc-b", "section": "Summary"},
            {"document_id": "doc-a", "section": "Root Cause"},
            {"document_id": "doc-b", "section": "Root Cause"},
        ]

        batches = group_by_document(chunks, max_batch_size=1)

        assert len(batches) == 2
        for document_id in ("doc-a", "doc-b"):
            containing = [
                batch for batch in batches if any(c["document_id"] == document_id for c in batch)
            ]
            assert len(containing) == 1
            assert all(c["document_id"] == document_id for c in containing[0])

    def test_packs_multiple_small_documents_into_one_batch_up_to_the_size_cap(self) -> None:
        chunks = [
            {"document_id": "doc-a", "section": "s1"},
            {"document_id": "doc-a", "section": "s2"},
            {"document_id": "doc-b", "section": "s1"},
            {"document_id": "doc-b", "section": "s2"},
        ]

        batches = group_by_document(chunks, max_batch_size=4)

        assert batches == [chunks]

    def test_a_document_larger_than_the_cap_still_gets_its_own_batch(self) -> None:
        chunks = [{"document_id": "doc-a", "section": str(i)} for i in range(5)]

        batches = group_by_document(chunks, max_batch_size=2)

        assert batches == [chunks]

    def test_preserves_first_appearance_order(self) -> None:
        chunks = [
            {"document_id": "doc-b", "section": "s"},
            {"document_id": "doc-a", "section": "s"},
        ]

        batches = group_by_document(chunks, max_batch_size=1)

        assert [batch[0]["document_id"] for batch in batches] == ["doc-b", "doc-a"]

    def test_empty_input_produces_no_batches(self) -> None:
        assert group_by_document([]) == []


class TestRLMBudget:
    def test_reserves_up_to_the_limit_then_refuses(self) -> None:
        budget = RLMBudget(max_depth=2, max_total_sub_agent_calls=2)

        assert budget.try_reserve_sub_agent_call() is True
        assert budget.try_reserve_sub_agent_call() is True
        assert budget.try_reserve_sub_agent_call() is False
        assert budget.sub_agent_calls_made == 2

    def test_reserves_the_rerank_call_exactly_once(self) -> None:
        """CLAUDE.md's architecture invariant: reranking runs at most once per user turn,
        never per RLM sub-agent."""
        budget = RLMBudget(max_depth=2, max_total_sub_agent_calls=2)

        assert budget.try_reserve_rerank() is True
        assert budget.try_reserve_rerank() is False
        assert budget.try_reserve_rerank() is False


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

    async def test_search_with_a_department_merges_a_scoped_and_unscoped_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26: a plain all-department search lets
        RRF fusion bury the right department's chunks under irrelevant ones. With a department
        set, `search` must run *both* a scoped and an all-department search and keep every
        scoped hit, exactly like `agents/nodes/retrieval.py`'s own merge."""
        scoped_chunk = _chunk("scoped::hit")
        unscoped_chunk = _chunk("unscoped::hit", department="security")
        calls: list[dict[str, Any]] = []

        async def _fake_hybrid_search(
            store: object,
            *,
            query_text: str,
            role: str,
            top_k: int,
            namespaces: list[str] | None = None,
        ) -> list[RetrievedChunk]:
            calls.append({"namespaces": namespaces, "top_k": top_k})
            return [scoped_chunk] if namespaces else [unscoped_chunk]

        monkeypatch.setattr(api, "hybrid_search", _fake_hybrid_search)
        context = _context(department="payments")

        outcome = await run_sandboxed(
            "result = search('payment outages', top_k=5)",
            injected_globals=build_rlm_globals(context),
            timeout_seconds=2.0,
        )

        assert {c["chunk_id"] for c in outcome.result} == {"scoped::hit", "unscoped::hit"}
        scoped_calls = [c for c in calls if c["namespaces"]]
        assert scoped_calls and all(c["namespaces"] == ["payments"] for c in scoped_calls)
        assert any(c["namespaces"] is None for c in calls)  # the all-department search too

    async def test_search_without_a_department_never_scopes_by_namespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No department identified — behavior must stay exactly the pre-fix plain search, one
        call, no `namespaces` kwarg at all."""
        calls: list[dict[str, Any]] = []

        async def _fake_hybrid_search(
            store: object, *, query_text: str, role: str, top_k: int
        ) -> list[RetrievedChunk]:
            calls.append({"top_k": top_k})
            return [_chunk()]

        monkeypatch.setattr(api, "hybrid_search", _fake_hybrid_search)
        context = _context(department=None)

        await run_sandboxed(
            "result = search('q', top_k=5)",
            injected_globals=build_rlm_globals(context),
            timeout_seconds=2.0,
        )

        assert len(calls) == 1

    async def test_search_reranks_once_when_the_budget_allows_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Parity fix: before this, `search` never reranked at all, unlike
        `agents/nodes/retrieval.py`. Gated through `RLMBudget.try_reserve_rerank` — the once-
        per-turn invariant CLAUDE.md names explicitly."""
        rerank_calls: list[str] = []

        async def _fake_hybrid_search(
            store: object, *, query_text: str, role: str, top_k: int
        ) -> list[RetrievedChunk]:
            return [_chunk("a"), _chunk("b")]

        async def _fake_rerank_chunks(
            store: object, *, query: str, chunks: list[RetrievedChunk], settings: object
        ) -> list[RetrievedChunk]:
            rerank_calls.append(query)
            return list(reversed(chunks))

        monkeypatch.setattr(api, "hybrid_search", _fake_hybrid_search)
        monkeypatch.setattr(api, "rerank_chunks", _fake_rerank_chunks)
        context = _context(settings=Settings(_env_file=None, rerank_enabled=True))

        outcome = await run_sandboxed(
            "result = search('payment outages', top_k=5)",
            injected_globals=build_rlm_globals(context),
            timeout_seconds=2.0,
        )

        assert rerank_calls == ["payment outages"]
        assert [c["chunk_id"] for c in outcome.result] == [
            "b",
            "a",
        ]  # reversed by the fake reranker
        assert context.budget.reranked is True

    async def test_search_never_reranks_twice_in_the_same_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second `search()` call sharing the same `RLMBudget` — e.g. a nested sub-agent's own
        search, or a generated plan calling `search` more than once — must not spend a second
        rerank call. The second call's chunks come back in raw (unreversed) RRF order."""
        rerank_calls = 0

        async def _fake_hybrid_search(
            store: object, *, query_text: str, role: str, top_k: int
        ) -> list[RetrievedChunk]:
            return [_chunk("a")]

        async def _fake_rerank_chunks(
            store: object, *, query: str, chunks: list[RetrievedChunk], settings: object
        ) -> list[RetrievedChunk]:
            nonlocal rerank_calls
            rerank_calls += 1
            return chunks

        monkeypatch.setattr(api, "hybrid_search", _fake_hybrid_search)
        monkeypatch.setattr(api, "rerank_chunks", _fake_rerank_chunks)
        context = _context(settings=Settings(_env_file=None, rerank_enabled=True))
        search = build_search(context)
        loop = asyncio.get_running_loop()

        await loop.run_in_executor(None, search, "first")
        await loop.run_in_executor(None, search, "second")

        assert rerank_calls == 1

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

    async def test_aggregate_requests_a_lower_temperature_than_the_provider_default(self) -> None:
        """`aggregate` should synthesize the evidence it is handed, not explore alternative
        phrasings of it (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 28's second addendum) —
        verifies the wiring actually asks for that, not just that the feature exists somewhere."""
        llm = _FakeLLM(results=[AggregatedFindings(summary="[A] cause one", recurring_themes=[])])
        context = _context(llm=llm)

        await run_sandboxed(
            "result = aggregate(data, 'q')",
            injected_globals={**build_rlm_globals(context), "data": ["[A] cause one"]},
            timeout_seconds=2.0,
        )

        assert llm.temperatures_requested == [api._AGGREGATE_TEMPERATURE]


class TestAggregateCompletenessCheck:
    """`build_aggregate`'s post-aggregation completeness guard: does the synthesized `summary`
    still mention every `[Title]` citation the raw sub-agent findings actually contained? This
    is deliberately narrower than a correctness check — it can only catch evidence a sub-agent
    *returned* that aggregation then dropped, never evidence the search/planning stages failed
    to retrieve in the first place (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 28's second
    addendum names this distinction explicitly)."""

    _FINDINGS: ClassVar[list[str]] = ["[Incident A] root cause X", "[Incident B] root cause Y"]

    async def test_no_retry_when_the_summary_already_preserves_every_citation(self) -> None:
        llm = _FakeLLM(
            results=[
                AggregatedFindings(
                    summary="[Incident A]: cause X. [Incident B]: cause Y.",
                    recurring_themes=[],
                )
            ]
        )
        context = _context(llm=llm)

        outcome = await run_sandboxed(
            "result = aggregate(data, 'q')",
            injected_globals={**build_rlm_globals(context), "data": self._FINDINGS},
            timeout_seconds=2.0,
        )

        assert llm.calls == 1
        assert outcome.result["summary"] == "[Incident A]: cause X. [Incident B]: cause Y."

    async def test_retries_once_and_uses_the_improved_result_when_a_citation_is_dropped(
        self,
    ) -> None:
        llm = _FakeLLM(
            results=[
                AggregatedFindings(summary="[Incident A]: cause X.", recurring_themes=[]),
                AggregatedFindings(
                    summary="[Incident A]: cause X. [Incident B]: cause Y.",
                    recurring_themes=["cause X"],
                ),
            ]
        )
        context = _context(llm=llm)

        outcome = await run_sandboxed(
            "result = aggregate(data, 'q')",
            injected_globals={**build_rlm_globals(context), "data": self._FINDINGS},
            timeout_seconds=2.0,
        )

        assert llm.calls == 2
        assert outcome.result == {
            "summary": "[Incident A]: cause X. [Incident B]: cause Y.",
            "recurring_themes": ["cause X"],
        }
        # The retry still asked for the same low, deterministic temperature, not the provider
        # default.
        assert llm.temperatures_requested == [api._AGGREGATE_TEMPERATURE] * 2

    async def test_retry_that_does_not_improve_falls_back_to_the_original_result(self) -> None:
        """Bounded: a retry that is no more complete than the first attempt must not be
        preferred over it, and must not trigger a second retry — exactly 2 calls, never more."""
        llm = _FakeLLM(
            results=[
                AggregatedFindings(summary="[Incident A]: cause X.", recurring_themes=[]),
                AggregatedFindings(
                    summary="[Incident A]: cause X, still incomplete.", recurring_themes=[]
                ),
            ]
        )
        context = _context(llm=llm)

        outcome = await run_sandboxed(
            "result = aggregate(data, 'q')",
            injected_globals={**build_rlm_globals(context), "data": self._FINDINGS},
            timeout_seconds=2.0,
        )

        assert llm.calls == 2
        assert outcome.result == {"summary": "[Incident A]: cause X.", "recurring_themes": []}

    async def test_retry_call_failure_gracefully_falls_back_to_the_original_result(self) -> None:
        """The retry attempt itself can fail (timeout, model unavailable) without losing the
        first attempt's already-validated, if incomplete, result — matches every other
        degradation in this codebase (a bad situation gets worse gracefully, never a hard
        failure this deep in a research turn)."""
        llm = _FakeLLM(
            results=[AggregatedFindings(summary="[Incident A]: cause X.", recurring_themes=[])],
            error=LLMTimeoutError("timed out"),
            fail_on_call=2,
        )
        context = _context(llm=llm)

        outcome = await run_sandboxed(
            "result = aggregate(data, 'q')",
            injected_globals={**build_rlm_globals(context), "data": self._FINDINGS},
            timeout_seconds=2.0,
        )

        assert llm.calls == 2
        assert outcome.result == {"summary": "[Incident A]: cause X.", "recurring_themes": []}


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
