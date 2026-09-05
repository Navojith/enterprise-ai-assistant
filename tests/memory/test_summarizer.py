"""Tests for `memory/summarizer.py`. `needs_summarization` is pure and tested directly at its
boundary; `summarize_oldest` is tested against a fake `LLMProvider` — no real Ollama call — the
same "fake the protocol, don't mock internals" approach `tests/agents/nodes/` uses for nodes
that depend on `LLMProvider`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    BaseMessageChunk,
    HumanMessage,
)
from pydantic import BaseModel

from backend.app.llm.provider import SchemaT
from backend.app.memory.summarizer import SummaryUpdate, needs_summarization, summarize_oldest


class _FakeLLM:
    """Minimal `LLMProvider`: scripted `astructured`, `astream` unused by these tests but
    implemented so this satisfies the full protocol `summarize_oldest` is typed against.
    Parameter types match `LLMProvider`'s own (`Sequence[BaseMessage]`), not the narrower
    `AnyMessage` union `summarize_oldest` happens to pass — a Protocol implementation must
    accept everything the protocol promises callers can pass, not just what one caller does.
    """

    def __init__(self, response: BaseModel) -> None:
        self._response = response
        self.last_messages: Sequence[BaseMessage] | None = None

    async def astructured(
        self, messages: Sequence[BaseMessage], *, schema: type[SchemaT], reasoning: bool = False
    ) -> SchemaT:
        self.last_messages = messages
        assert isinstance(self._response, schema)
        return self._response

    async def astream(
        self, messages: Sequence[BaseMessage], *, reasoning: bool = True
    ) -> AsyncIterator[BaseMessageChunk]:
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator function


class TestNeedsSummarization:
    def test_below_the_threshold_does_not_trigger(self) -> None:
        assert needs_summarization(message_count=5, max_verbatim_messages=12) is False

    def test_exactly_at_the_threshold_does_not_trigger(self) -> None:
        assert needs_summarization(message_count=12, max_verbatim_messages=12) is False

    def test_one_past_the_threshold_triggers(self) -> None:
        assert needs_summarization(message_count=13, max_verbatim_messages=12) is True


class TestSummarizeOldest:
    async def test_folds_the_oldest_batch_and_returns_the_new_summary(self) -> None:
        messages: list[AnyMessage] = [
            HumanMessage(content="What is the payments SLA?", id="m1"),
            AIMessage(content="It is 99.9% uptime.", id="m2"),
            HumanMessage(content="And for core banking?", id="m3"),
        ]
        fake_llm = _FakeLLM(SummaryUpdate(summary="User asked about SLAs for two departments."))

        remove, summary = await summarize_oldest(
            messages=messages, existing_summary="", batch_size=2, llm=fake_llm
        )

        assert [message.id for message in remove] == ["m1", "m2"]
        assert summary == "User asked about SLAs for two departments."

    async def test_messages_without_an_id_are_excluded_from_the_remove_list(self) -> None:
        messages: list[AnyMessage] = [
            HumanMessage(content="No id here"),
            AIMessage(content="Also no id", id="m2"),
        ]
        fake_llm = _FakeLLM(SummaryUpdate(summary="updated"))

        remove, _ = await summarize_oldest(
            messages=messages, existing_summary="", batch_size=2, llm=fake_llm
        )

        assert [message.id for message in remove] == ["m2"]

    async def test_the_prior_summary_is_included_in_the_prompt(self) -> None:
        messages: list[AnyMessage] = [HumanMessage(content="follow-up question", id="m1")]
        fake_llm = _FakeLLM(SummaryUpdate(summary="updated"))

        await summarize_oldest(
            messages=messages, existing_summary="Prior summary text.", batch_size=1, llm=fake_llm
        )

        assert fake_llm.last_messages is not None
        prompt_text = " ".join(str(message.content) for message in fake_llm.last_messages)
        assert "Prior summary text." in prompt_text
