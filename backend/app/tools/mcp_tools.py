"""Registry `ToolSpec`s for the three MCP-backed capabilities ASSESSMENT.md names: the employee
directory, the service catalog, and incident records.

One `ToolSpec` per dataset here, each wrapping *two* of `mcp_server/server.py`'s six tools (a
filtered list, and a lookup by id/name) behind one typed parameter model — the model's caller
does not need to know the underlying MCP server exposes them as separate tools; that is a
server-side implementation detail this module hides, the same way `knowledge_search.py` hides
whether retrieval used dense, sparse, or both. All three require `Permission.MCP_TOOLS`
(`ASSESSMENT.md`'s RBAC table: Analyst and Administrator only) — enforced by
`tools/registry.py::execute`, not by anything in this module or in `mcp_server/`.

**A shape discovered by running the real client against the real server, not documented
anywhere obvious:** the `mcp` SDK's structured-output machinery
(`mcp.server.mcpserver.utilities.func_metadata._create_output_model`) can only validate a JSON
*object* as a tool's top-level structured output, so a tool whose return annotation is a `list`
(every `list_employees`/`list_services`/`list_incidents` in `mcp_server/server.py`) gets
silently wrapped server-side as `{"result": [...]}}`, while a tool returning `dict[str, Any]`
(every `get_*`) is not wrapped at all. `_unwrap_list` below undoes that wrapping on this
module's own three "list" call sites specifically — not in `tools/mcp_client.py`, which stays a
generic client with no opinion on any particular server's return shapes.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, Field

from backend.app.core.security.rbac import Permission, Principal
from backend.app.tools.mcp_client import MCPClient
from backend.app.tools.registry import ToolResult, ToolSpec


def _unwrap_list(data: Any) -> Any:
    """Undo the `{"result": [...]}}` wrapping described in this module's docstring. Only
    matches that exact shape — anything else (an error dict, an already-bare list) passes
    through unchanged, so this is safe to call unconditionally on a "list" tool's result."""
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


class EmployeeDirectoryParams(BaseModel):
    department: str | None = Field(
        default=None, description="Filter to one department, e.g. 'payments'. Omit to list all."
    )
    employee_id: str | None = Field(
        default=None, description="Look up one employee by id instead of listing."
    )


class ServiceCatalogParams(BaseModel):
    department: str | None = Field(
        default=None, description="Filter to one owning department. Omit to list all."
    )
    service_name: str | None = Field(
        default=None, description="Look up one service by name instead of listing."
    )


class IncidentRecordsParams(BaseModel):
    department: str | None = Field(default=None, description="Filter to one department.")
    severity: str | None = Field(
        default=None, description="Filter to one severity ('low', 'medium', 'high')."
    )
    incident_id: str | None = Field(
        default=None, description="Look up one incident by id instead of listing."
    )


def build_employee_directory_tool(client: MCPClient) -> ToolSpec:
    async def _handle(principal: Principal, raw_params: BaseModel) -> ToolResult:
        params = cast(EmployeeDirectoryParams, raw_params)
        if params.employee_id:
            data = await client.call_tool("get_employee", {"employee_id": params.employee_id})
        else:
            data = _unwrap_list(
                await client.call_tool("list_employees", {"department": params.department})
            )
        return ToolResult(summary=f"Employee directory result: {data}", data=data)

    return ToolSpec(
        name="employee_directory",
        description="Look up bank employees by department or employee id.",
        required_permission=Permission.MCP_TOOLS,
        params_schema=EmployeeDirectoryParams,
        handler=_handle,
    )


def build_service_catalog_tool(client: MCPClient) -> ToolSpec:
    async def _handle(principal: Principal, raw_params: BaseModel) -> ToolResult:
        params = cast(ServiceCatalogParams, raw_params)
        if params.service_name:
            data = await client.call_tool("get_service", {"service_name": params.service_name})
        else:
            data = _unwrap_list(
                await client.call_tool("list_services", {"department": params.department})
            )
        return ToolResult(summary=f"Service catalog result: {data}", data=data)

    return ToolSpec(
        name="service_catalog",
        description="Look up bank services by owning department or service name.",
        required_permission=Permission.MCP_TOOLS,
        params_schema=ServiceCatalogParams,
        handler=_handle,
    )


def build_incident_records_tool(client: MCPClient) -> ToolSpec:
    async def _handle(principal: Principal, raw_params: BaseModel) -> ToolResult:
        params = cast(IncidentRecordsParams, raw_params)
        if params.incident_id:
            data = await client.call_tool("get_incident", {"incident_id": params.incident_id})
        else:
            data = _unwrap_list(
                await client.call_tool(
                    "list_incidents",
                    {"department": params.department, "severity": params.severity},
                )
            )
        return ToolResult(summary=f"Incident records result: {data}", data=data)

    return ToolSpec(
        name="incident_records",
        description="Look up bank incident records by department, severity, or incident id.",
        required_permission=Permission.MCP_TOOLS,
        params_schema=IncidentRecordsParams,
        handler=_handle,
    )
