"""Dummy enterprise data the MCP server exposes: an employee directory, a service catalog, and
incident records — the three examples ASSESSMENT.md names under "MCP Tool" verbatim.

Static, in-memory, deterministic — this is a demo fixture for a low-priority requirement
("not a high priority requirement" per the brief), not a system needing persistence or
mutation. Department names match `backend.app.retrieval.models.DEPARTMENTS` so a question that
spans both the document corpus and this directory (e.g. "who owns the payments incident
process?") stays internally consistent.
"""

from __future__ import annotations

# `typing_extensions.TypedDict`, not `typing.TypedDict` — pydantic v2's schema generation (used
# by `mcp.server.mcpserver.MCPServer.tool()` to build each tool's structured-output schema from
# its return annotation) requires the `typing_extensions` version on Python < 3.12; this
# project is pinned to Python 3.11 (`pyproject.toml`). `typing.TypedDict` decorates a plain
# function fine and type-checks fine under mypy, so this only fails at *runtime*, the first
# time `mcp_server/server.py`'s `@server.tool()` decorators actually run — found by running
# `mcp_server`'s own tests, not by mypy or ruff.
from typing_extensions import TypedDict


class Employee(TypedDict):
    employee_id: str
    name: str
    department: str
    title: str
    email: str


class Service(TypedDict):
    service_name: str
    owning_department: str
    status: str
    description: str


class IncidentRecord(TypedDict):
    incident_id: str
    title: str
    department: str
    severity: str
    status: str
    created_date: str


EMPLOYEES: tuple[Employee, ...] = (
    {
        "employee_id": "E-1001",
        "name": "Priya Nandan",
        "department": "payments",
        "title": "Payments Engineering Lead",
        "email": "priya.nandan@example-bank.com",
    },
    {
        "employee_id": "E-1002",
        "name": "Marcus Ondieki",
        "department": "core_banking",
        "title": "Core Banking Platform Manager",
        "email": "marcus.ondieki@example-bank.com",
    },
    {
        "employee_id": "E-1003",
        "name": "Sofia Reyes",
        "department": "security",
        "title": "Head of Security Operations",
        "email": "sofia.reyes@example-bank.com",
    },
    {
        "employee_id": "E-1004",
        "name": "Daniel Achebe",
        "department": "human_resources",
        "title": "HR Business Partner",
        "email": "daniel.achebe@example-bank.com",
    },
    {
        "employee_id": "E-1005",
        "name": "Wei Lin Tan",
        "department": "product",
        "title": "Senior Product Manager",
        "email": "weilin.tan@example-bank.com",
    },
    {
        "employee_id": "E-1006",
        "name": "Grace Muthoni",
        "department": "customer_support",
        "title": "Customer Support Manager",
        "email": "grace.muthoni@example-bank.com",
    },
)

SERVICES: tuple[Service, ...] = (
    {
        "service_name": "payment-gateway",
        "owning_department": "payments",
        "status": "operational",
        "description": "Processes inbound and outbound payment transactions.",
    },
    {
        "service_name": "core-ledger",
        "owning_department": "core_banking",
        "status": "operational",
        "description": "System of record for account balances and postings.",
    },
    {
        "service_name": "fraud-detection",
        "owning_department": "security",
        "status": "degraded",
        "description": "Real-time transaction risk scoring.",
    },
    {
        "service_name": "customer-portal",
        "owning_department": "product",
        "status": "operational",
        "description": "Customer-facing web and mobile banking application.",
    },
    {
        "service_name": "support-ticketing",
        "owning_department": "customer_support",
        "status": "operational",
        "description": "Internal case management for customer support agents.",
    },
)

INCIDENTS: tuple[IncidentRecord, ...] = (
    {
        "incident_id": "INC-2041",
        "title": "Payment gateway timeout spike during batch settlement",
        "department": "payments",
        "severity": "high",
        "status": "resolved",
        "created_date": "2025-11-03",
    },
    {
        "incident_id": "INC-2058",
        "title": "Fraud detection false-positive rate elevated",
        "department": "security",
        "severity": "medium",
        "status": "monitoring",
        "created_date": "2025-12-14",
    },
    {
        "incident_id": "INC-2077",
        "title": "Core ledger reconciliation job delayed",
        "department": "core_banking",
        "severity": "medium",
        "status": "resolved",
        "created_date": "2026-01-22",
    },
    {
        "incident_id": "INC-2093",
        "title": "Customer portal login latency degradation",
        "department": "product",
        "severity": "low",
        "status": "resolved",
        "created_date": "2026-02-09",
    },
)
