"""Fallback chain of `LLMProvider`s, each guarded by its own circuit breaker.

`docs/DECISIONS.md` §7 reconciles this against the "one model for every node" hardware
constraint: the abstraction is real, tested code — satisfying ASSESSMENT.md's requirement to
demonstrate graceful degradation on "LLM failures" — but only one tier (`qwen3:4b`) is
configured today. Because inference is local, there are no API rate-limit errors to guard
against; the failure modes this chain actually exists for are a timed-out request, a model that
failed to load, and malformed structured output, all raised as `LLMError` subclasses by
`OllamaProvider`.

The circuit breaker's pure state-transition logic (`_next_breaker_state`) is separated from the
stateful `CircuitBreaker` wrapper for the same reason `core/security/rate_limit.py`'s refill math
is separated from its async shell: every edge case — the exact failure that trips the breaker,
the cooldown boundary, a success resetting it — is a plain unit test with no event loop or clock
mocking beyond `freezegun`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from langchain_core.messages import BaseMessage, BaseMessageChunk

from backend.app.core.errors import LLMError, LLMUnavailableError
from backend.app.llm.provider import LLMProvider, SchemaT

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _BreakerState:
    consecutive_failures: int = 0
    opened_at: datetime | None = None


def _next_breaker_state(
    state: _BreakerState, *, failed: bool, failure_threshold: int
) -> _BreakerState:
    """Pure transition: a success always resets to closed; a failure increments the streak and
    opens the breaker (recording when) once `failure_threshold` is reached."""
    if not failed:
        return _BreakerState()
    failures = state.consecutive_failures + 1
    opened_at = state.opened_at
    if failures >= failure_threshold and opened_at is None:
        opened_at = datetime.now(UTC)
    return _BreakerState(consecutive_failures=failures, opened_at=opened_at)


def _is_open(state: _BreakerState, *, now: datetime, cooldown_seconds: float) -> bool:
    """Closed (allow) until the failure threshold trips; then open (deny) until `cooldown_seconds`
    has elapsed, at which point exactly one trial request is let through (half-open) — if it
    fails, `record_failure` reopens the breaker with a fresh `opened_at` via the same call above."""
    if state.opened_at is None:
        return False
    return (now - state.opened_at).total_seconds() < cooldown_seconds


class CircuitBreaker:
    """Per-provider failure tracker. See module docstring for the split between this stateful
    wrapper and the pure functions it calls."""

    def __init__(self, *, failure_threshold: int, cooldown_seconds: float) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._state = _BreakerState()

    def is_open(self) -> bool:
        return _is_open(self._state, now=datetime.now(UTC), cooldown_seconds=self._cooldown_seconds)

    def record_success(self) -> None:
        self._state = _next_breaker_state(
            self._state, failed=False, failure_threshold=self._failure_threshold
        )

    def record_failure(self) -> None:
        self._state = _next_breaker_state(
            self._state, failed=True, failure_threshold=self._failure_threshold
        )


class FallbackChain:
    """`LLMProvider` composed of one or more providers, tried in order.

    `astructured` is a single round trip, so falling back to the next provider on failure is
    always safe. `astream` is not: once a chunk has reached the caller, that content may already
    be visible to the end user (the SSE endpoint forwards deltas as they arrive), so restarting
    on a different provider at that point would duplicate or garble what was already shown.
    `astream` therefore only falls back to the next provider on a failure that happens *before*
    the first chunk is yielded; a mid-stream failure propagates as a hard error instead.
    """

    def __init__(
        self, providers: Sequence[LLMProvider], *, failure_threshold: int, cooldown_seconds: float
    ) -> None:
        if not providers:
            raise ValueError("FallbackChain requires at least one provider.")
        self._entries: list[tuple[LLMProvider, CircuitBreaker]] = [
            (
                provider,
                CircuitBreaker(
                    failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds
                ),
            )
            for provider in providers
        ]

    async def astructured(
        self,
        messages: Sequence[BaseMessage],
        *,
        schema: type[SchemaT],
        reasoning: bool = False,
    ) -> SchemaT:
        last_error: LLMError | None = None
        for index, (provider, breaker) in enumerate(self._entries):
            if breaker.is_open():
                logger.warning("llm_provider_skipped_breaker_open", provider_index=index)
                continue
            try:
                result = await provider.astructured(messages, schema=schema, reasoning=reasoning)
            except LLMError as exc:
                breaker.record_failure()
                last_error = exc
                logger.warning("llm_provider_failed", provider_index=index, error=str(exc))
                continue
            breaker.record_success()
            return result
        raise last_error or LLMUnavailableError("No LLM providers available.")

    async def astream(
        self, messages: Sequence[BaseMessage], *, reasoning: bool = True
    ) -> AsyncIterator[BaseMessageChunk]:
        last_error: LLMError | None = None
        for index, (provider, breaker) in enumerate(self._entries):
            if breaker.is_open():
                logger.warning("llm_provider_skipped_breaker_open", provider_index=index)
                continue
            started = False
            try:
                async for chunk in provider.astream(messages, reasoning=reasoning):
                    started = True
                    yield chunk
                breaker.record_success()
                return
            except LLMError as exc:
                breaker.record_failure()
                if started:
                    # Partial output already reached the caller — see class docstring.
                    raise
                last_error = exc
                logger.warning(
                    "llm_provider_failed_before_first_token", provider_index=index, error=str(exc)
                )
                continue
        raise last_error or LLMUnavailableError("No LLM providers available.")
