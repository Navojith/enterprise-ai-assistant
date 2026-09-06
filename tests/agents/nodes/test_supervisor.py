"""Tests for `_available_tool_categories`, the pure helper `supervisor_node` uses to describe
what tool categories a role has to the routing prompt. `supervisor_node` itself is not
unit-tested directly — see `tests/agents/nodes/test_tools.py`'s module docstring for why."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from backend.app.agents.nodes.supervisor import (
    _SYSTEM_PROMPT_TEMPLATE,
    _available_tool_categories,
    _build_routing_schema,
)
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

    def test_the_system_prompt_template_grounds_the_current_date(self) -> None:
        """`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 31: live-verified that without this,
        `qwen3:4b`'s own stale, pre-cutoff sense of "now" routed a real question about the
        bank's own 2026 data to `"direct"`, reasoning the year "hasn't happened yet". Guards
        against the `{current_date}` placeholder (filled by `supervisor_node` from
        `agents/prompting.py::current_date_context`) being accidentally removed."""
        assert "{current_date}" in _SYSTEM_PROMPT_TEMPLATE

    def test_the_analytics_category_warns_it_cannot_retrieve_data(self) -> None:
        """`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 29's second finding: a "Run a Python
        analysis to count..." style question routed here despite there being no real data in
        the conversation to compute over, and the model then fabricated a dataset rather than
        admit it had nothing to work with. The category text must steer this kind of question
        toward retrieval/research instead of merely naming "Python analysis" as a capability."""
        text = _available_tool_categories([_spec(Permission.ANALYTICS_TOOLS)])

        assert "already stated in this conversation" in text
        assert "cannot search or retrieve" in text


def _decision(**overrides: object) -> dict[str, object]:
    """A complete, valid `RoutingDecision` payload, with any field replaceable — pass `None`
    for a field to omit it entirely, so a test can check that field is actually required
    rather than merely accepting `None` as its value."""
    fields: dict[str, object] = {
        "reasoning": "because",
        "route": "direct",
        "search_query": "q",
        "department": "unclear",
    }
    fields.update(overrides)
    return {key: value for key, value in fields.items() if value is not None}


class TestBuildRoutingSchema:
    """`"research"` must be a structurally impossible value for a principal the Supervisor did
    not offer it to — bind-time filtering, mirroring `tests/agents/nodes/test_tools.py`'s
    `TestBuildChoiceSchema` for tool names, applied here to a route."""

    def test_research_is_rejected_when_not_included(self) -> None:
        schema = _build_routing_schema(include_research=False)

        with pytest.raises(ValidationError):
            schema.model_validate(_decision(route="research"))

    def test_research_validates_when_included(self) -> None:
        schema = _build_routing_schema(include_research=True)

        instance = schema.model_validate(_decision(route="research"))
        assert instance.route == "research"  # type: ignore[attr-defined]

    def test_the_base_routes_always_validate_either_way(self) -> None:
        for include_research in (False, True):
            schema = _build_routing_schema(include_research=include_research)
            for route in ("retrieval", "direct", "tools"):
                instance = schema.model_validate(_decision(route=route))
                assert instance.route == route  # type: ignore[attr-defined]

    def test_reasoning_is_required(self) -> None:
        schema = _build_routing_schema(include_research=False)

        with pytest.raises(ValidationError):
            schema.model_validate(_decision(reasoning=None))

    def test_search_query_is_required(self) -> None:
        """`search_query` is what lets `retrieval_node`/`research_node` resolve a follow-up
        like "what are the response steps in that document?" into a standalone query — see
        `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26. Missing it should fail validation the
        same way a missing `reasoning` does, not silently default to an empty string."""
        schema = _build_routing_schema(include_research=False)

        with pytest.raises(ValidationError):
            schema.model_validate(_decision(search_query=None))

    def test_department_is_required(self) -> None:
        """`department` is what lets `retrieval_node` scope `hybrid_search` to one namespace —
        see `docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 26. Missing it should fail validation
        rather than silently searching every department."""
        schema = _build_routing_schema(include_research=False)

        with pytest.raises(ValidationError):
            schema.model_validate(_decision(department=None))

    def test_department_accepts_a_real_department_or_unclear(self) -> None:
        schema = _build_routing_schema(include_research=False)

        for department in ("payments", "security", "unclear"):
            instance = schema.model_validate(_decision(department=department))
            assert instance.department == department  # type: ignore[attr-defined]

    def test_department_rejects_an_unknown_value(self) -> None:
        schema = _build_routing_schema(include_research=False)

        with pytest.raises(ValidationError):
            schema.model_validate(_decision(department="not-a-real-department"))
