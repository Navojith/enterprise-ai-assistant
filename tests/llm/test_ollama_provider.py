"""Tests for `llm/ollama_provider.py::OllamaProvider.astructured`'s retry behavior.

No real Ollama call: `self._model` (a real `ChatOllama`) is replaced with a fake exposing the
two things `_call_non_streaming` actually calls — `_chat_params(...)` and
`_async_client.chat(...)` — scripted to raise or return on each successive call, the same style
as `tests/llm/test_chain.py`'s `_FakeProvider`, one layer further in.

The retry-on-timeout path, and the non-streaming call itself, exist because of a real finding
from containerizing this project (docs/ASSUMPTIONS_AND_TRADEOFFS.md trade-off 24): a container
reaching this project's intentionally-native Ollama over Docker Desktop's `host.docker.internal`
NAT can hit stretches where `ChatOllama`'s always-streamed internal request stalls; a
non-streaming call measurably avoids most of that, and the retry is a second line of defense on
top of it, not the primary mitigation — see the trade-off entry for the full, honest picture.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel

from backend.app.core.config import Settings
from backend.app.core.errors import LLMTimeoutError
from backend.app.llm.ollama_provider import OllamaProvider


class _Schema(BaseModel):
    value: str


class _FakeAsyncClient:
    """Stands in for `ChatOllama._async_client`. `side_effects` is consumed one per call: an
    `Exception` instance is raised, a plain string is wrapped as the response's message content
    (the shape `_call_non_streaming` actually reads)."""

    def __init__(self, side_effects: list[Exception | str]) -> None:
        self._side_effects = list(side_effects)
        self.calls = 0

    async def chat(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        effect = self._side_effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return {"message": {"content": effect}}


class _FakeChatOllama:
    """Stands in for `self._model`. `_chat_params` just echoes back enough of a real request
    dict for `_call_non_streaming` to log/pass through (`model`, `think`) — the actual
    parameter-building logic belongs to the real `ChatOllama` and isn't under test here."""

    def __init__(self, async_client: _FakeAsyncClient) -> None:
        self._async_client = async_client

    def _chat_params(self, messages: list[BaseMessage], **kwargs: Any) -> dict[str, Any]:
        return {"model": "qwen3:4b", "think": kwargs.get("reasoning", False)}


def _make_provider(async_client: _FakeAsyncClient) -> OllamaProvider:
    # A generous, not tiny, timeout: `_call_non_streaming` now awaits a real (no-op, since no
    # LangSmith callbacks are configured in a test) callback-manager round trip on every call,
    # and a near-zero timeout here raced that scheduling overhead rather than testing retry logic.
    # A simulated `TimeoutError()` in `side_effects` still raises instantly regardless.
    settings = Settings(_env_file=None, llm_request_timeout_seconds=5.0)
    provider = OllamaProvider(settings)
    provider._model = _FakeChatOllama(async_client)  # type: ignore[assignment]
    return provider


_MESSAGES: Sequence[BaseMessage] = [HumanMessage(content="hi")]


@pytest.mark.asyncio
async def test_a_timed_out_first_attempt_is_retried_and_can_still_succeed() -> None:
    """The exact shape found live: the first attempt hangs (TimeoutError), a second, fresh
    attempt succeeds — the turn should succeed, not fail, on the retry."""
    client = _FakeAsyncClient([TimeoutError(), '{"value": "ok"}'])
    provider = _make_provider(client)

    result = await provider.astructured(_MESSAGES, schema=_Schema)

    assert result == _Schema(value="ok")
    assert client.calls == 2


@pytest.mark.asyncio
async def test_timeouts_on_every_attempt_raise_llm_timeout_error() -> None:
    """Exhausting the retry budget on timeouts alone still surfaces as `LLMTimeoutError` —
    not the generic malformed-output `LLMError` — so the SSE event keeps its `llm_timeout` code
    and the fallback chain's timeout-specific handling still applies."""
    client = _FakeAsyncClient([TimeoutError(), TimeoutError()])
    provider = _make_provider(client)

    with pytest.raises(LLMTimeoutError):
        await provider.astructured(_MESSAGES, schema=_Schema)

    assert client.calls == 2


@pytest.mark.asyncio
async def test_a_single_timeout_does_not_consume_the_malformed_output_retry_message() -> None:
    """A timeout followed by a malformed response still exhausts cleanly with the generic
    schema-conformance error, not a timeout error — the two failure classes stay distinguishable
    in the final exception even though they share one retry budget."""
    client = _FakeAsyncClient([TimeoutError(), "not valid json"])
    provider = _make_provider(client)

    with pytest.raises(Exception, match="schema-conformant"):
        await provider.astructured(_MESSAGES, schema=_Schema)


@pytest.mark.asyncio
async def test_malformed_json_on_the_first_attempt_is_retried_and_can_still_succeed() -> None:
    """Fails safely on malformed output by retrying once, same as the pre-existing behavior for
    a schema grammar that occasionally slips — this is not a new failure mode, just re-pinned
    against the new non-streaming call path."""
    client = _FakeAsyncClient(["not valid json", '{"value": "ok"}'])
    provider = _make_provider(client)

    result = await provider.astructured(_MESSAGES, schema=_Schema)

    assert result == _Schema(value="ok")
    assert client.calls == 2
