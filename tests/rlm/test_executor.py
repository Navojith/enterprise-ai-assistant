"""Tests for `rlm/executor.py`'s pure-ish logic: `_stringify`, and `_run_plan_at_depth`/
`run_research`'s generate-then-run-then-fallback shape. `execute_research` itself calls
`get_stream_writer()` and is not unit-tested directly — matching
`tests/agents/nodes/test_tools.py`'s documented precedent for every function in this codebase
that only works inside a real LangGraph node invocation; it is verified live end to end instead
(`docs/PROGRESS.md`'s session log).
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator, Sequence
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import BaseMessage, BaseMessageChunk

from backend.app.core.security.rbac import Principal, Role
from backend.app.llm.provider import SchemaT
from backend.app.rlm import api, executor
from backend.app.rlm.api import RLMBudget, RLMContext
from backend.app.rlm.executor import _run_plan_at_depth, _stringify, run_research


class _FakeLLM:
    """Only ever reached by `sub_agent`'s leaf case or `aggregate` when the deterministic
    fallback plan runs in these tests — a single canned finding/summary is enough for both.
    Structurally matches `LLMProvider` (including `astream`, unused here) the same way
    `tests/llm/test_chain.py::_FakeProvider` does."""

    async def astructured(
        self, messages: Sequence[BaseMessage], *, schema: type[SchemaT], reasoning: bool = False
    ) -> SchemaT:
        if schema.__name__ == "SubAgentFinding":
            return schema(reasoning="r", finding="a finding")
        return schema(summary="a summary", recurring_themes=[])

    async def astream(
        self, messages: Sequence[BaseMessage], *, reasoning: bool = True
    ) -> AsyncIterator[BaseMessageChunk]:
        raise NotImplementedError("rlm/executor.py never calls astream")
        yield  # pragma: no cover - makes this an async generator for typing purposes


def _context(*, run_nested_plan: Any = None) -> RLMContext:
    async def _default_nested(*, question: str, data: Any, context: RLMContext) -> str:
        return "nested finding"

    return RLMContext(
        loop=asyncio.get_running_loop(),
        captured_vars=contextvars.copy_context(),
        principal=Principal(username="analyst-1", role=Role.ANALYST),
        role="analyst",
        store=AsyncMock(),
        llm=_FakeLLM(),
        budget=RLMBudget(max_depth=2, max_total_sub_agent_calls=4),
        depth=0,
        max_concurrent_sub_agents=4,
        run_nested_plan=run_nested_plan or _default_nested,
    )


class TestStringify:
    def test_unwraps_a_summary_dict(self) -> None:
        assert _stringify({"summary": "the answer", "recurring_themes": ["x"]}) == "the answer"

    def test_stringifies_anything_else_as_is(self) -> None:
        assert _stringify(["a", "b"]) == "['a', 'b']"
        assert _stringify(None) == "None"


class TestRunPlanAtDepth:
    async def test_a_valid_generated_plan_runs_without_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(executor, "generate_plan", _fake_generate_plan("result = 1 + 1", False))
        context = _context()

        result, used_fallback = await _run_plan_at_depth(
            question="q", data=None, context=context, timeout_seconds=2.0
        )

        assert result == 2
        assert used_fallback is False

    async def test_a_generated_plan_that_fails_at_runtime_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(executor, "generate_plan", _fake_generate_plan("result = 1 / 0", False))

        async def _fake_hybrid_search(*args: object, **kwargs: object) -> list[object]:
            return []

        monkeypatch.setattr(api, "hybrid_search", _fake_hybrid_search)
        context = _context()

        result, used_fallback = await _run_plan_at_depth(
            question="q", data=None, context=context, timeout_seconds=5.0
        )

        assert used_fallback is True
        assert result == {"summary": "a summary", "recurring_themes": []}

    async def test_a_failing_fallback_plan_is_not_retried_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `used_fallback=True` straight from `generate_plan` simulates the fallback plan itself
        # (fixed, hand-written code) somehow still failing at runtime — `_run_plan_at_depth`
        # must not try a second fallback in that case, since a bug in fixed code is a real bug.
        monkeypatch.setattr(executor, "generate_plan", _fake_generate_plan("result = 1 / 0", True))
        context = _context()

        with pytest.raises(Exception, match="ZeroDivisionError"):
            await _run_plan_at_depth(question="q", data=None, context=context, timeout_seconds=2.0)


class TestRunResearch:
    async def test_returns_the_stringified_result_of_a_nested_cycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            executor,
            "generate_plan",
            _fake_generate_plan(
                'result = {"summary": "nested summary", "recurring_themes": []}', False
            ),
        )
        context = _context()

        finding = await run_research(question="sub-question", data=[], context=context)

        assert finding == "nested summary"


def _fake_generate_plan(code: str, used_fallback: bool) -> Any:
    async def _fake(question: str, *, llm: object) -> tuple[str, bool]:
        return code, used_fallback

    return _fake
