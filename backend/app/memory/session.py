"""Builds the message list a node sends to the LLM from persisted state.

One function, so the Supervisor and Response nodes turn `(system prompt, rolling summary,
recent messages)` into a prompt identically instead of each re-deriving "how much history to
include" on its own — the single place `docs/DECISIONS.md`'s memory design actually executes.
"""

from __future__ import annotations

from langchain_core.messages import AnyMessage, SystemMessage


def build_context_messages(
    *, system_prompt: str, summary: str, recent_messages: list[AnyMessage]
) -> list[AnyMessage]:
    preamble = system_prompt
    if summary:
        preamble = f"{preamble}\n\nSummary of earlier conversation:\n{summary}"
    return [SystemMessage(content=preamble), *recent_messages]
