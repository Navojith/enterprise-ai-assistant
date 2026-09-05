"""Assembles the process's one `ToolRegistry` from every tool this cycle defines.

A separate module from `registry.py` itself so that module stays free of importing every
concrete tool (`knowledge_search`, `python_analysis`, the MCP-backed tools) — the same
separation `agents/graph.py` keeps between "what a graph looks like" and "what nodes it wires
in". `main.py`'s lifespan calls this once, after `pinecone_store` and `mcp_client` are known
(or known to be unavailable), and stores the result on `GraphContext` alongside them.
"""

from __future__ import annotations

from backend.app.core.config import Settings
from backend.app.retrieval.pinecone_store import PineconeStore
from backend.app.tools.knowledge_search import build_knowledge_search_tool
from backend.app.tools.mcp_client import MCPClient
from backend.app.tools.mcp_tools import (
    build_employee_directory_tool,
    build_incident_records_tool,
    build_service_catalog_tool,
)
from backend.app.tools.python_analysis import build_python_analysis_tool
from backend.app.tools.registry import ToolRegistry, ToolSpec


def build_default_registry(
    *, pinecone_store: PineconeStore | None, mcp_client: MCPClient | None, settings: Settings
) -> ToolRegistry:
    """`pinecone_store=None` or `mcp_client=None` (Pinecone or the MCP server unreachable at
    startup) each drop their dependent tools from the registry entirely, rather than
    registering a tool that would only fail at call time — `available_to` then never offers a
    Supervisor the tool in the first place, matching `docs/ARCHITECTURE.md`'s "MCP server down
    -> tool marked unavailable, supervisor routes around it".
    """
    specs: list[ToolSpec] = [
        build_python_analysis_tool(timeout_seconds=settings.sandbox_timeout_seconds)
    ]
    if pinecone_store is not None:
        specs.append(build_knowledge_search_tool(pinecone_store, settings))
    if mcp_client is not None:
        specs.extend(
            [
                build_employee_directory_tool(mcp_client),
                build_service_catalog_tool(mcp_client),
                build_incident_records_tool(mcp_client),
            ]
        )
    return ToolRegistry(specs)
