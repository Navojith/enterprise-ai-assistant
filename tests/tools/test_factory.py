"""Tests for `tools/factory.py::build_default_registry`'s degradation behavior — when Pinecone
or the MCP server is unavailable at startup, their dependent tools should simply not exist in
the registry, matching `docs/ARCHITECTURE.md`'s "MCP server down -> tool marked unavailable,
supervisor routes around it" (never a tool that exists but always errors).

An Administrator principal (every permission) is used throughout purely to read back
`available_to(...)` as "every tool this registry actually holds" — RBAC filtering itself is
`tests/tools/test_registry.py`'s job, not this module's.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from backend.app.core.config import get_settings
from backend.app.core.security.rbac import Principal, Role
from backend.app.tools.factory import build_default_registry
from backend.app.tools.mcp_client import MCPClient
from backend.app.tools.registry import ToolRegistry

_ADMIN = Principal(username="admin", role=Role.ADMINISTRATOR)


def _registered_names(registry: ToolRegistry) -> set[str]:
    return {spec.name for spec in registry.available_to(_ADMIN)}


class TestBuildDefaultRegistry:
    def test_python_analysis_is_always_present(self) -> None:
        registry = build_default_registry(
            pinecone_store=None, mcp_client=None, settings=get_settings()
        )

        assert "python_analysis" in _registered_names(registry)

    def test_knowledge_search_is_omitted_when_pinecone_is_unavailable(self) -> None:
        registry = build_default_registry(
            pinecone_store=None, mcp_client=None, settings=get_settings()
        )

        assert "knowledge_search" not in _registered_names(registry)

    def test_knowledge_search_is_present_when_pinecone_is_available(self) -> None:
        registry = build_default_registry(
            pinecone_store=AsyncMock(), mcp_client=None, settings=get_settings()
        )

        assert "knowledge_search" in _registered_names(registry)

    def test_mcp_tools_are_omitted_when_the_mcp_client_is_unavailable(self) -> None:
        registry = build_default_registry(
            pinecone_store=None, mcp_client=None, settings=get_settings()
        )

        names = _registered_names(registry)
        assert names.isdisjoint({"employee_directory", "service_catalog", "incident_records"})

    def test_mcp_tools_are_present_when_the_mcp_client_is_available(self) -> None:
        registry = build_default_registry(
            pinecone_store=None, mcp_client=AsyncMock(spec=MCPClient), settings=get_settings()
        )

        names = _registered_names(registry)
        assert {"employee_directory", "service_catalog", "incident_records"} <= names
