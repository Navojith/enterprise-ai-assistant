"""The MCP server: `mcp.server.mcpserver.MCPServer` exposing the dummy employee directory,
service catalog, and incident records over Streamable HTTP.

Built on the official `mcp` SDK — see `docs/ASSUMPTIONS_AND_TRADEOFFS.md` assumption 7 and
trade-off 15 for why that is `mcp.server.mcpserver.MCPServer` in the pinned `mcp==2.1.1`, not
the `mcp.server.fastmcp.FastMCP` name `docs/ARCHITECTURE.md`'s shorthand ("FastMCP") could also
suggest — that module name was renamed and repurposed as a migration-guide pointer in the SDK's
2.x line, discovered by importing the installed package rather than assumed from its 1.x shape.

Six tools, two per dataset (a filtered list, and a lookup by id/name) — kept as plain functions
with primitive parameter types so the SDK's own schema generation produces a small, LLM-legible
input schema with no custom validation needed here; `backend/app/tools/mcp_client.py` and
`backend/app/tools/mcp_tools.py` are what give these RBAC and a typed Python interface on the
caller's side. This module has no authorization of its own — by design, per `docs/DECISIONS.md`
§6, RBAC is enforced once, at the backend's tool-execution boundary, not duplicated here.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver.server import MCPServer

from mcp_server.config import get_mcp_server_settings
from mcp_server.data import EMPLOYEES, INCIDENTS, SERVICES, Employee, IncidentRecord, Service

server = MCPServer(
    name="enterprise-dummy-data",
    instructions=(
        "Dummy enterprise data for a commercial bank: an employee directory, a service "
        "catalog, and incident records. All data is synthetic."
    ),
)


@server.tool()
def list_employees(department: str | None = None) -> list[Employee]:
    """List employees, optionally filtered to one department."""
    if department is None:
        return list(EMPLOYEES)
    return [e for e in EMPLOYEES if e["department"] == department]


@server.tool()
def get_employee(employee_id: str) -> dict[str, Any]:
    """Look up one employee by id."""
    for employee in EMPLOYEES:
        if employee["employee_id"] == employee_id:
            return dict(employee)
    return {"error": f"No employee with id {employee_id!r}."}


@server.tool()
def list_services(department: str | None = None) -> list[Service]:
    """List catalog services, optionally filtered to one owning department."""
    if department is None:
        return list(SERVICES)
    return [s for s in SERVICES if s["owning_department"] == department]


@server.tool()
def get_service(service_name: str) -> dict[str, Any]:
    """Look up one service by name."""
    for service in SERVICES:
        if service["service_name"] == service_name:
            return dict(service)
    return {"error": f"No service named {service_name!r}."}


@server.tool()
def list_incidents(
    department: str | None = None, severity: str | None = None
) -> list[IncidentRecord]:
    """List incident records, optionally filtered by department and/or severity."""
    results = list(INCIDENTS)
    if department is not None:
        results = [i for i in results if i["department"] == department]
    if severity is not None:
        results = [i for i in results if i["severity"] == severity]
    return results


@server.tool()
def get_incident(incident_id: str) -> dict[str, Any]:
    """Look up one incident record by id."""
    for incident in INCIDENTS:
        if incident["incident_id"] == incident_id:
            return dict(incident)
    return {"error": f"No incident with id {incident_id!r}."}


def run() -> None:
    """Entry point for `python -m mcp_server`. Streamable HTTP, not stdio — this process runs
    standalone in its own terminal (`docs/SETUP.md` step 4), not spawned as a child of the
    backend, so the backend connects to it over HTTP the same way it would to any other network
    dependency (Pinecone, Ollama). Reads its own minimal `MCPServerSettings`
    (`mcp_server/config.py`), not the backend's `Settings` — this process has no other need for
    anything the backend imports, and importing it anyway was a real coupling bug; see that
    module's docstring."""
    settings = get_mcp_server_settings()
    server.run(
        transport="streamable-http",
        host=settings.mcp_server_host,
        port=settings.mcp_server_port,
        streamable_http_path=settings.mcp_server_path,
    )
