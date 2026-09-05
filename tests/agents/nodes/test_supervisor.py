"""Tests for `_available_tool_categories`, the pure helper `supervisor_node` uses to describe
what tool categories a role has to the routing prompt. `supervisor_node` itself is not
unit-tested directly — see `tests/agents/nodes/test_tools.py`'s module docstring for why."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from backend.app.agents.nodes.supervisor import _available_tool_categories, _build_routing_schema
from backend.app.core.security.rbac import Permission
from backend.app.tools.registry import ToolResult, ToolSpec


class _NoopParams(BaseModel):
    pass


async def _noop_handler(principal: object, params: BaseModel) -> ToolResult:
    return ToolResult(summary="noop")


def _spec(permission: Permission, name: str = "tool") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="d",
        required_permission=permission,
        params_schema=_NoopParams,
        handler=_noop_handler,
    )


class TestAvailableToolCategories:
    def test_no_tools_produces_the_explicit_none_available_message(self) -> None:
        assert _available_tool_categories([]) == "no tools are available for this request"

    def test_a_search_only_offering_names_only_that_category(self) -> None:
        text = _available_tool_categories([_spec(Permission.SEARCH)])

        assert "internal-document search" in text
        assert "Python analysis" not in text
        assert "employee" not in text

    def test_every_permission_produces_every_category_once(self) -> None:
        text = _available_tool_categories(
            [
                _spec(Permission.SEARCH, "a"),
                _spec(Permission.ANALYTICS_TOOLS, "b"),
                _spec(Permission.MCP_TOOLS, "c"),
            ]
        )

        assert "internal-document search" in text
        assert "Python analysis" in text
        assert "employee" in text

    def test_two_tools_sharing_a_permission_do_not_duplicate_the_category(self) -> None:
        text = _available_tool_categories(
            [_spec(Permission.ANALYTICS_TOOLS, "a"), _spec(Permission.ANALYTICS_TOOLS, "b")]
        )

        assert text.count("Python analysis") == 1


class TestBuildRoutingSchema:
    """`"research"` must be a structurally impossible value for a principal the Supervisor did
    not offer it to — bind-time filtering, mirroring `tests/agents/nodes/test_tools.py`'s
    `TestBuildChoiceSchema` for tool names, applied here to a route."""

    def test_research_is_rejected_when_not_included(self) -> None:
        schema = _build_routing_schema(include_research=False)

        with pytest.raises(ValidationError):
            schema.model_validate({"reasoning": "because", "route": "research"})

    def test_research_validates_when_included(self) -> None:
        schema = _build_routing_schema(include_research=True)

        instance = schema.model_validate({"reasoning": "because", "route": "research"})
        assert instance.route == "research"  # type: ignore[attr-defined]

    def test_the_base_routes_always_validate_either_way(self) -> None:
        for include_research in (False, True):
            schema = _build_routing_schema(include_research=include_research)
            for route in ("retrieval", "direct", "tools"):
                instance = schema.model_validate({"reasoning": "because", "route": route})
                assert instance.route == route  # type: ignore[attr-defined]

    def test_reasoning_is_required(self) -> None:
        schema = _build_routing_schema(include_research=False)

        with pytest.raises(ValidationError):
            schema.model_validate({"route": "direct"})
