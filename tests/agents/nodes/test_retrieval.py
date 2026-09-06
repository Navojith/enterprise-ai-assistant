"""Tests for `_latest_user_text`, the pure, `get_stream_writer`-free helper in
`agents/nodes/retrieval.py`.

`merge_prioritizing_scoped` — the department-scoped/all-department merge this node also uses —
is tested in `tests/retrieval/test_hybrid.py` instead, alongside the module it actually lives in
(`retrieval/hybrid.py`) now that `rlm/api.py` shares it too.

`retrieval_node` itself is not unit-tested directly, matching `tests/agents/nodes/
test_tools.py`'s precedent: `get_stream_writer()` raises `RuntimeError` outside a real graph
invocation, so every node in this codebase is verified live end to end rather than by faking
that context — only the logic a node delegates to a plain function gets a unit test.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from backend.app.agents.nodes.retrieval import _latest_user_text


class TestLatestUserText:
    def test_returns_the_most_recent_human_message(self) -> None:
        messages: list[AnyMessage] = [
            HumanMessage(content="first question"),
            AIMessage(content="an answer"),
            HumanMessage(content="second question"),
        ]

        assert _latest_user_text(messages) == "second question"

    def test_raises_when_there_is_no_human_message(self) -> None:
        messages: list[AnyMessage] = [AIMessage(content="an answer")]

        with pytest.raises(ValueError, match="no user message"):
            _latest_user_text(messages)
