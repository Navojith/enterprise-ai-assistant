"""Tests for `mcp_server/server.py`'s tool functions — plain functions, called directly rather
than through a live MCP session, since MCP protocol framing itself is the SDK's concern, not
this module's. `backend/app/tools/test_mcp_client.py` and `test_mcp_tools.py` cover the client
side of the protocol boundary.
"""

from __future__ import annotations

from mcp_server.server import (
    get_employee,
    get_incident,
    get_service,
    list_employees,
    list_incidents,
    list_services,
)


class TestEmployeeDirectory:
    def test_list_employees_with_no_filter_returns_everyone(self) -> None:
        assert len(list_employees()) >= 6

    def test_list_employees_filters_by_department(self) -> None:
        results = list_employees(department="payments")

        assert results
        assert all(e["department"] == "payments" for e in results)

    def test_get_employee_finds_a_known_id(self) -> None:
        employee = get_employee("E-1001")

        assert employee["name"]
        assert "error" not in employee

    def test_get_employee_reports_an_unknown_id_as_data_not_an_exception(self) -> None:
        result = get_employee("does-not-exist")

        assert "error" in result


class TestServiceCatalog:
    def test_list_services_filters_by_department(self) -> None:
        results = list_services(department="security")

        assert results
        assert all(s["owning_department"] == "security" for s in results)

    def test_get_service_finds_a_known_name(self) -> None:
        service = get_service("payment-gateway")

        assert "error" not in service

    def test_get_service_reports_an_unknown_name_as_data(self) -> None:
        result = get_service("does-not-exist")

        assert "error" in result


class TestIncidentRecords:
    def test_list_incidents_filters_by_both_department_and_severity(self) -> None:
        results = list_incidents(department="payments", severity="high")

        assert all(i["department"] == "payments" and i["severity"] == "high" for i in results)

    def test_get_incident_finds_a_known_id(self) -> None:
        incident = get_incident("INC-2041")

        assert "error" not in incident

    def test_get_incident_reports_an_unknown_id_as_data(self) -> None:
        result = get_incident("does-not-exist")

        assert "error" in result
