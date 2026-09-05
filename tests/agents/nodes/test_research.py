"""Tests for the pure, `get_stream_writer`-free helpers in `agents/nodes/research.py`.

`research_node` itself is not unit-tested directly, matching `tests/agents/nodes/test_tools.py`'s
precedent: `get_stream_writer()` raises `RuntimeError` outside a real graph invocation, so every
node in this codebase is verified live end to end rather than by faking that context — only the
logic a node delegates to a plain function gets a unit test.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from backend.app.agents.nodes.research import _latest_user_text, _stringify_research_result


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


class TestStringifyResearchResult:
    def test_a_summary_only_dict_is_returned_as_is(self) -> None:
        result = _stringify_research_result({"summary": "root cause: timeouts"})

        assert result == "root cause: timeouts"

    def test_a_summary_with_recurring_themes_appends_them(self) -> None:
        result = _stringify_research_result(
            {"summary": "root cause: timeouts", "recurring_themes": ["timeout", "retry storm"]}
        )

        assert "root cause: timeouts" in result
        assert "timeout, retry storm" in result

    def test_a_dict_without_a_summary_key_is_stringified_wholesale(self) -> None:
        result = _stringify_research_result({"other": "shape"})

        assert result == "{'other': 'shape'}"

    def test_a_non_dict_result_is_stringified(self) -> None:
        assert _stringify_research_result("already a string") == "already a string"
