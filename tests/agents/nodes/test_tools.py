"""Tests for the pure, `get_stream_writer`-free helpers in `agents/nodes/tools.py`.

`tools_node` itself is not unit-tested directly, matching `tests/agents/nodes/test_validator.py`'s
precedent: LangGraph's `get_stream_writer()` raises `RuntimeError` outside a real graph
invocation (`Called get_config outside of a runnable context`), so every node in this codebase
is verified live end to end (`docs/PROGRESS.md`'s session log) rather than by faking that
context — only the logic a node delegates to a plain function gets a unit test.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from backend.app.agents.nodes.tools import _build_choice_schema, _describe_tools
from backend.app.core.security.rbac import Permission
from backend.app.tools.registry import ToolResult, ToolSpec


class _NoopParams(BaseModel):
    pass


async def _noop_handler(principal: object, params: BaseModel) -> ToolResult:
    return ToolResult(summary="noop")


def _spec(name: str, description: str = "does a thing") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        required_permission=Permission.SEARCH,
        params_schema=_NoopParams,
        handler=_noop_handler,
    )


class TestDescribeTools:
    def test_lists_every_spec_by_name_and_description(self) -> None:
        text = _describe_tools([_spec("alpha", "does alpha"), _spec("beta", "does beta")])

        assert "alpha: does alpha" in text
        assert "beta: does beta" in text

    def test_empty_list_produces_empty_text(self) -> None:
        assert _describe_tools([]) == ""


class TestBuildChoiceSchema:
    def test_only_the_given_tool_names_validate(self) -> None:
        schema = _build_choice_schema([_spec("alpha"), _spec("beta")])

        instance = schema.model_validate({"reasoning": "because", "tool_name": "alpha"})
        assert instance.tool_name == "alpha"  # type: ignore[attr-defined]

    def test_a_tool_name_outside_the_offered_set_is_rejected(self) -> None:
        schema = _build_choice_schema([_spec("alpha")])

        with pytest.raises(ValidationError):
            schema.model_validate({"reasoning": "because", "tool_name": "not_offered"})

    def test_reasoning_is_required(self) -> None:
        schema = _build_choice_schema([_spec("alpha")])

        with pytest.raises(ValidationError):
            schema.model_validate({"tool_name": "alpha"})
