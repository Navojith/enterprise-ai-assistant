"""Tests for the Python Analysis tool's wiring — `rlm/sandbox.py` itself is tested exhaustively
in `tests/rlm/test_sandbox.py`; these confirm the tool spec plugs into it correctly (RBAC,
parameter shape, and that a sandbox violation surfaces as a tool failure, not a crash)."""

from __future__ import annotations

import pytest

from backend.app.core.errors import ToolExecutionError, ToolNotPermittedError
from backend.app.core.security.rbac import Permission, Principal, Role
from backend.app.tools.python_analysis import build_python_analysis_tool
from backend.app.tools.registry import ToolRegistry

_ANALYST = Principal(username="a", role=Role.ANALYST)
_VIEWER = Principal(username="v", role=Role.VIEWER)


def _registry() -> ToolRegistry:
    return ToolRegistry([build_python_analysis_tool(timeout_seconds=1.0)])


class TestPythonAnalysisTool:
    def test_requires_analytics_tools_permission(self) -> None:
        spec = build_python_analysis_tool(timeout_seconds=1.0)

        assert spec.required_permission == Permission.ANALYTICS_TOOLS

    async def test_runs_code_over_injected_data(self) -> None:
        registry = _registry()

        result = await registry.execute(
            "python_analysis",
            {"code": "result = sum(data)", "data": [1, 2, 3]},
            principal=_ANALYST,
        )

        assert result.data == 6

    async def test_a_viewer_cannot_call_it(self) -> None:
        registry = _registry()

        with pytest.raises(ToolNotPermittedError):
            await registry.execute("python_analysis", {"code": "result = 1"}, principal=_VIEWER)

    async def test_a_sandbox_violation_surfaces_as_a_tool_execution_error(self) -> None:
        registry = _registry()

        with pytest.raises(ToolExecutionError):
            await registry.execute("python_analysis", {"code": "import os"}, principal=_ANALYST)

    def test_the_tool_description_warns_it_cannot_retrieve_data(self) -> None:
        """`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 29's second finding: live testing found
        the model, routed here with no real data available, fabricated an entirely invented
        dataset rather than admit it had nothing to analyze. This description (shown at both
        tool-choice and fill-args time, `agents/nodes/tools.py`) is the safety net for whenever
        the Supervisor's own routing prompt still sends a question here that needs it."""
        spec = build_python_analysis_tool(timeout_seconds=1.0)

        assert "Cannot search, retrieve, or look up" in spec.description

    def test_the_field_descriptions_forbid_inventing_placeholder_data(self) -> None:
        schema = build_python_analysis_tool(timeout_seconds=1.0).params_schema.model_fields
        code_description = schema["code"].description
        data_description = schema["data"].description

        assert code_description is not None and "do NOT invent" in code_description
        assert (
            data_description is not None and "cannot search or retrieve records" in data_description
        )
