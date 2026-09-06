"""Tests for `rlm/planner.py`: the deterministic fallback plan's own validity, and
`generate_plan`'s three-stage shape (accept / retry-once-with-feedback / fall back) against a
scripted `LLMProvider`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from langchain_core.messages import BaseMessage, BaseMessageChunk

from backend.app.core.errors import LLMUnavailableError
from backend.app.llm.provider import SchemaT
from backend.app.rlm.planner import ResearchPlan, deterministic_fallback_plan, generate_plan
from backend.app.rlm.sandbox import validate_ast


class _FakeLLM:
    """Returns each of `plans` in turn, one per call — lets a test script exactly what the
    model produces on the first attempt and, if it retries, the second. Structurally matches
    `LLMProvider` (including `astream`, unused here) the same way
    `tests/llm/test_chain.py::_FakeProvider` does."""

    def __init__(
        self, plans: list[ResearchPlan] | None = None, error: Exception | None = None
    ) -> None:
        self._plans = list(plans or [])
        self._error = error
        self.calls = 0

    async def astructured(
        self,
        messages: Sequence[BaseMessage],
        *,
        schema: type[SchemaT],
        reasoning: bool = False,
        temperature: float | None = None,
    ) -> SchemaT:
        self.calls += 1
        if self._error is not None:
            raise self._error
        plan = self._plans[self.calls - 1]
        assert isinstance(plan, schema)
        return plan

    async def astream(
        self, messages: Sequence[BaseMessage], *, reasoning: bool = True
    ) -> AsyncIterator[BaseMessageChunk]:
        raise NotImplementedError("rlm/planner.py never calls astream")
        yield  # pragma: no cover - makes this an async generator for typing purposes


class TestDeterministicFallbackPlan:
    def test_passes_the_ast_allowlist(self) -> None:
        validate_ast(deterministic_fallback_plan("what happened?"))

    def test_assigns_result_and_uses_only_the_curated_api(self) -> None:
        code = deterministic_fallback_plan("what happened?")

        assert "result" in code
        for name in ("search", "group_by_document", "sub_agents", "aggregate"):
            assert name in code
        for forbidden in ("import ", "open(", "exec(", "eval("):
            assert forbidden not in code


class TestGeneratePlan:
    async def test_a_valid_first_attempt_is_used_as_is(self) -> None:
        llm = _FakeLLM(plans=[ResearchPlan(reasoning="strategy", code="result = search('q')")])

        code, used_fallback = await generate_plan("what happened?", llm=llm)

        assert code == "result = search('q')"
        assert used_fallback is False
        assert llm.calls == 1

    async def test_an_invalid_first_attempt_is_retried_once_with_feedback(self) -> None:
        llm = _FakeLLM(
            plans=[
                ResearchPlan(reasoning="strategy", code="import os\nresult = 1"),
                ResearchPlan(reasoning="fixed", code="result = search('q')"),
            ]
        )

        code, used_fallback = await generate_plan("what happened?", llm=llm)

        assert code == "result = search('q')"
        assert used_fallback is False
        assert llm.calls == 2

    async def test_two_invalid_attempts_fall_back_to_the_deterministic_plan(self) -> None:
        llm = _FakeLLM(
            plans=[
                ResearchPlan(reasoning="strategy", code="import os\nresult = 1"),
                ResearchPlan(reasoning="still broken", code="exec('1')"),
            ]
        )

        code, used_fallback = await generate_plan("what happened?", llm=llm)

        assert code == deterministic_fallback_plan("what happened?")
        assert used_fallback is True
        assert llm.calls == 2

    async def test_an_unavailable_model_falls_back_immediately_without_a_retry(self) -> None:
        llm = _FakeLLM(error=LLMUnavailableError("Ollama is down"))

        code, used_fallback = await generate_plan("what happened?", llm=llm)

        assert code == deterministic_fallback_plan("what happened?")
        assert used_fallback is True
        assert llm.calls == 1
