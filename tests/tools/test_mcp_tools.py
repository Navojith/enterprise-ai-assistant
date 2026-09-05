"""Tests for the three MCP-backed `ToolSpec`s — RBAC gating and correct dispatch between the
"list" and "look up by id/name" shapes each wraps. `tools/mcp_client.py` itself is faked out
entirely; these only verify this module's own logic."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.app.core.errors import ToolNotPermittedError
from backend.app.core.security.rbac import Permission, Principal, Role
from backend.app.tools.mcp_tools import (
    build_employee_directory_tool,
    build_incident_records_tool,
    build_service_catalog_tool,
)
from backend.app.tools.registry import ToolRegistry

_VIEWER = Principal(username="v", role=Role.VIEWER)
_ANALYST = Principal(username="a", role=Role.ANALYST)


def _fake_client(return_value: Any = None) -> AsyncMock:
    client = AsyncMock()
    client.call_tool = AsyncMock(return_value=return_value)
    return client


class TestEmployeeDirectoryTool:
    def test_requires_mcp_tools_permission(self) -> None:
        spec = build_employee_directory_tool(_fake_client())

        assert spec.required_permission == Permission.MCP_TOOLS

    async def test_a_viewer_cannot_call_it(self) -> None:
        registry = ToolRegistry([build_employee_directory_tool(_fake_client())])

        with pytest.raises(ToolNotPermittedError):
            await registry.execute("employee_directory", {}, principal=_VIEWER)

    async def test_listing_calls_list_employees_and_unwraps_the_result_envelope(self) -> None:
        # The MCP server wraps a `list`-returning tool's structured output as
        # `{"result": [...]}}` — see `mcp_tools.py`'s module docstring; this fake reproduces
        # that real, live-verified shape rather than the plain list a naive fake might return.
        client = _fake_client({"result": [{"employee_id": "E-1"}]})
        registry = ToolRegistry([build_employee_directory_tool(client)])

        result = await registry.execute(
            "employee_directory", {"department": "payments"}, principal=_ANALYST
        )

        client.call_tool.assert_awaited_once_with("list_employees", {"department": "payments"})
        assert result.data == [{"employee_id": "E-1"}]

    async def test_looking_up_by_id_calls_get_employee(self) -> None:
        client = _fake_client({"employee_id": "E-1"})
        registry = ToolRegistry([build_employee_directory_tool(client)])

        await registry.execute("employee_directory", {"employee_id": "E-1"}, principal=_ANALYST)

        client.call_tool.assert_awaited_once_with("get_employee", {"employee_id": "E-1"})


class TestServiceCatalogTool:
    async def test_looking_up_by_name_calls_get_service(self) -> None:
        client = _fake_client({"service_name": "payment-gateway"})
        registry = ToolRegistry([build_service_catalog_tool(client)])

        await registry.execute(
            "service_catalog", {"service_name": "payment-gateway"}, principal=_ANALYST
        )

        client.call_tool.assert_awaited_once_with(
            "get_service", {"service_name": "payment-gateway"}
        )


class TestIncidentRecordsTool:
    async def test_listing_passes_both_filters(self) -> None:
        client = _fake_client([])
        registry = ToolRegistry([build_incident_records_tool(client)])

        await registry.execute(
            "incident_records",
            {"department": "payments", "severity": "high"},
            principal=_ANALYST,
        )

        client.call_tool.assert_awaited_once_with(
            "list_incidents", {"department": "payments", "severity": "high"}
        )

    async def test_looking_up_by_id_calls_get_incident(self) -> None:
        client = _fake_client({"incident_id": "INC-1"})
        registry = ToolRegistry([build_incident_records_tool(client)])

        await registry.execute("incident_records", {"incident_id": "INC-1"}, principal=_ANALYST)

        client.call_tool.assert_awaited_once_with("get_incident", {"incident_id": "INC-1"})
