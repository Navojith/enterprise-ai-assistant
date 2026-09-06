"""Tests for `llm/chain.py`: the circuit breaker's pure transition logic directly, and
`FallbackChain`'s provider-selection behavior against fake `LLMProvider`s — no real Ollama call,
matching `tests/core/security/test_rate_limit.py`'s split between pure math and async wiring."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import pytest
from freezegun import freeze_time
from langchain_core.messages import AIMessageChunk, BaseMessage, BaseMessageChunk, HumanMessage
from pydantic import BaseModel

from backend.app.core.errors import LLMTimeoutError, LLMUnavailableError
from backend.app.llm.chain import (
    CircuitBreaker,
    FallbackChain,
    _BreakerState,
    _is_open,
    _next_breaker_state,
)
from backend.app.llm.provider import SchemaT


class _Schema(BaseModel):
    value: str


class _FakeProvider:
    """Scripted `LLMProvider`: raises `astructured_error`/`astream_error` if set, else returns
    `astructured_result` / yields `astream_chunks`."""

    def __init__(
        self,
        *,
        astructured_result: BaseModel | None = None,
        astructured_error: Exception | None = None,
        astream_chunks: list[BaseMessageChunk] | None = None,
        astream_error: Exception | None = None,
        fail_after_chunks: int = 0,
    ) -> None:
        self._astructured_result = astructured_result
        self._astructured_error = astructured_error
        self._astream_chunks = astream_chunks or []
        self._astream_error = astream_error
        self._fail_after_chunks = fail_after_chunks
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
        if self._astructured_error:
            raise self._astructured_error
        assert self._astructured_result is not None
        assert isinstance(self._astructured_result, schema)
        return self._astructured_result

    async def astream(
        self, messages: Sequence[BaseMessage], *, reasoning: bool = True
    ) -> AsyncIterator[BaseMessageChunk]:
        self.calls += 1
        for index, chunk in enumerate(self._astream_chunks):
            if index == self._fail_after_chunks and self._astream_error:
                raise self._astream_error
            yield chunk
        if self._fail_after_chunks >= len(self._astream_chunks) and self._astream_error:
            raise self._astream_error


_MESSAGES = [HumanMessage(content="hi")]


class TestBreakerTransitions:
    def test_a_success_resets_to_the_closed_state(self) -> None:
        state = _BreakerState(consecutive_failures=5, opened_at=None)

        result = _next_breaker_state(state, failed=False, failure_threshold=3)

        assert result == _BreakerState()

    def test_failures_below_threshold_do_not_open_the_breaker(self) -> None:
        state = _BreakerState()
        state = _next_breaker_state(state, failed=True, failure_threshold=3)
        state = _next_breaker_state(state, failed=True, failure_threshold=3)

        assert state.consecutive_failures == 2
        assert state.opened_at is None

    def test_reaching_the_threshold_opens_the_breaker(self) -> None:
        state = _BreakerState(consecutive_failures=2)

        state = _next_breaker_state(state, failed=True, failure_threshold=3)

        assert state.consecutive_failures == 3
        assert state.opened_at is not None

    def test_is_open_before_the_cooldown_elapses(self) -> None:
        with freeze_time("2026-01-01T00:00:00Z") as frozen:
            opened_at = datetime.now(UTC)
            state = _BreakerState(consecutive_failures=3, opened_at=opened_at)
            frozen.tick(delta=10)

            assert _is_open(state, now=datetime.now(UTC), cooldown_seconds=30) is True

    def test_is_open_becomes_false_once_the_cooldown_elapses(self) -> None:
        with freeze_time("2026-01-01T00:00:00Z") as frozen:
            opened_at = datetime.now(UTC)
            state = _BreakerState(consecutive_failures=3, opened_at=opened_at)
            frozen.tick(delta=31)

            assert _is_open(state, now=datetime.now(UTC), cooldown_seconds=30) is False


class TestCircuitBreaker:
    def test_a_fresh_breaker_is_closed(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30)

        assert breaker.is_open() is False

    def test_opens_after_the_failure_threshold(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30)

        breaker.record_failure()
        assert breaker.is_open() is False
        breaker.record_failure()
        assert breaker.is_open() is True

    def test_a_success_closes_it_again(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=30)
        breaker.record_failure()
        assert breaker.is_open() is True

        breaker.record_success()

        assert breaker.is_open() is False


class TestFallbackChainStructured:
    async def test_returns_the_first_providers_result_when_it_succeeds(self) -> None:
        provider = _FakeProvider(astructured_result=_Schema(value="ok"))
        chain = FallbackChain([provider], failure_threshold=3, cooldown_seconds=30)

        result = await chain.astructured(_MESSAGES, schema=_Schema)

        assert result.value == "ok"

    async def test_falls_back_to_the_second_provider_on_failure(self) -> None:
        failing = _FakeProvider(astructured_error=LLMTimeoutError("timed out"))
        working = _FakeProvider(astructured_result=_Schema(value="from-second"))
        chain = FallbackChain([failing, working], failure_threshold=3, cooldown_seconds=30)

        result = await chain.astructured(_MESSAGES, schema=_Schema)

        assert result.value == "from-second"
        assert failing.calls == 1
        assert working.calls == 1

    async def test_raises_when_every_provider_fails(self) -> None:
        provider = _FakeProvider(astructured_error=LLMTimeoutError("timed out"))
        chain = FallbackChain([provider], failure_threshold=3, cooldown_seconds=30)

        with pytest.raises(LLMTimeoutError):
            await chain.astructured(_MESSAGES, schema=_Schema)

    async def test_an_open_breaker_skips_a_provider_without_calling_it(self) -> None:
        failing = _FakeProvider(astructured_error=LLMTimeoutError("timed out"))
        working = _FakeProvider(astructured_result=_Schema(value="ok"))
        chain = FallbackChain([failing, working], failure_threshold=1, cooldown_seconds=30)

        await chain.astructured(_MESSAGES, schema=_Schema)  # trips the first provider's breaker
        working.calls = 0
        failing.calls = 0

        await chain.astructured(_MESSAGES, schema=_Schema)

        assert failing.calls == 0  # skipped: breaker open
        assert working.calls == 1


class TestFallbackChainStream:
    async def test_streams_from_the_first_provider_when_it_succeeds(self) -> None:
        chunk = AIMessageChunk(content="hello")
        provider = _FakeProvider(astream_chunks=[chunk])
        chain = FallbackChain([provider], failure_threshold=3, cooldown_seconds=30)

        collected = [c async for c in chain.astream(_MESSAGES)]

        assert collected == [chunk]

    async def test_falls_back_before_any_chunk_is_yielded(self) -> None:
        failing = _FakeProvider(astream_chunks=[], astream_error=LLMUnavailableError("down"))
        chunk = AIMessageChunk(content="hello")
        working = _FakeProvider(astream_chunks=[chunk])
        chain = FallbackChain([failing, working], failure_threshold=3, cooldown_seconds=30)

        collected = [c async for c in chain.astream(_MESSAGES)]

        assert collected == [chunk]

    async def test_a_mid_stream_failure_after_a_chunk_was_yielded_is_not_retried(self) -> None:
        chunk = AIMessageChunk(content="partial")
        failing = _FakeProvider(
            astream_chunks=[chunk],
            astream_error=LLMUnavailableError("dropped"),
            fail_after_chunks=1,
        )
        working = _FakeProvider(astream_chunks=[AIMessageChunk(content="should not be used")])
        chain = FallbackChain([failing, working], failure_threshold=3, cooldown_seconds=30)

        collected: list[BaseMessageChunk] = []
        with pytest.raises(LLMUnavailableError):
            async for c in chain.astream(_MESSAGES):
                collected.append(c)

        assert collected == [chunk]
        assert working.calls == 0
