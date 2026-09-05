"""Rolling-summary session memory: bounds how much raw conversation history a thread accumulates.

ASSESSMENT.md requires memory to "survive multiple turns during a session" without saying the
context window must grow unboundedly to provide it — and on a 4B local model with a 4096-token
context (`docs/DECISIONS.md` §3), it structurally cannot. The design: keep the most recent
`memory_max_verbatim_messages` messages verbatim (so exact recent wording is available for
follow-up questions), and once that threshold is crossed, fold the oldest
`memory_summarize_batch_size` of them into a single rolling summary string via one more schema-
constrained LLM call, then drop those messages from state. `agents/state.py`'s `add_messages`
reducer is what makes "drop" possible — a message is removed by returning a `RemoveMessage`
carrying its id, LangGraph's documented mechanism for this exact pattern.

`needs_summarization` is a pure predicate, independent of `summarize_oldest`'s LLM call, so the
trigger boundary (`exactly at the threshold`, `one below`, `one above`) is a plain unit test.
"""

from __future__ import annotations

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from pydantic import BaseModel, Field

from backend.app.llm.provider import LLMProvider

_SUMMARY_SYSTEM_PROMPT = (
    "You maintain a running summary of an ongoing conversation between an employee and an "
    "internal AI assistant at a commercial bank. Update the summary to incorporate the new "
    "messages below, preserving names, decisions, and facts the user stated. Keep it to a few "
    "sentences — a summary, not a transcript."
)


class SummaryUpdate(BaseModel):
    summary: str = Field(description="The updated rolling summary, folding in the new messages.")


def needs_summarization(*, message_count: int, max_verbatim_messages: int) -> bool:
    """`True` once `message_count` exceeds the verbatim budget. A strict `>`, not `>=`, so a
    thread sitting exactly at the budget is left alone — summarization is triggered by growth
    past the limit, not by merely reaching it."""
    return message_count > max_verbatim_messages


def _format_for_summary(message: AnyMessage) -> str:
    if isinstance(message, HumanMessage):
        role = "user"
    elif isinstance(message, AIMessage):
        role = "assistant"
    else:
        role = "system"
    return f"{role}: {message.content}"


async def summarize_oldest(
    *,
    messages: list[AnyMessage],
    existing_summary: str,
    batch_size: int,
    llm: LLMProvider,
) -> tuple[list[RemoveMessage], str]:
    """Fold `messages[:batch_size]` into `existing_summary` and return the `RemoveMessage`s that
    drop them from state alongside the new summary text. Messages without an `id` (should not
    happen for anything that has passed through the `add_messages` reducer, which assigns one)
    are silently excluded from the remove list rather than raising — losing the ability to trim
    one stray message is a much smaller problem than crashing the turn over it.
    """
    to_fold = messages[:batch_size]
    transcript = "\n".join(_format_for_summary(message) for message in to_fold)
    prompt: list[AnyMessage] = [
        SystemMessage(content=_SUMMARY_SYSTEM_PROMPT),
        HumanMessage(
            content=f"Current summary:\n{existing_summary or '(none yet)'}\n\nNew messages:\n{transcript}"
        ),
    ]
    update = await llm.astructured(prompt, schema=SummaryUpdate, reasoning=False)
    remove = [RemoveMessage(id=message.id) for message in to_fold if message.id is not None]
    return remove, update.summary
