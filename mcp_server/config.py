"""Minimal, standalone settings for the MCP server process.

`mcp_server/server.py` used to import `backend.app.core.config.Settings` for exactly three
fields (`mcp_server_host`, `mcp_server_port`, `mcp_server_path`) — a real coupling bug, not an
untidy import: it pulled the entire backend dependency graph (FastAPI, LangGraph, Pinecone,
Postgres drivers, ...) and every one of `Settings`' env vars into a process this module's own
`run()` docstring already describes as standalone ("this process runs standalone in its own
terminal ... the backend connects to it over HTTP the same way it would to any other network
dependency"). It happened to work under `python -m mcp_server` (`-m` puts the current working
directory on `sys.path`) — the identical pattern broke the moment the *frontend* tried it under
`streamlit run` in Docker, which does not, surfacing `ModuleNotFoundError: No module named
'backend'` for a reason that had nothing to do with Streamlit and everything to do with two
independently-deployable services depending on each other's internals.

This class owns just the three settings the server itself binds with, reading the identical env
var names from the identical `.env` file as `backend/app/core/config.py::Settings` — a value set
once in `.env` still can't disagree between the two processes, without either one importing the
other. Not cached (`@lru_cache`) like the backend's `get_settings()`: this is read once at
process startup (`server.py::run()`), never per-request.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MCPServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
    )

    mcp_server_host: str = "127.0.0.1"
    mcp_server_port: int = Field(default=8100, gt=0, le=65535)
    mcp_server_path: str = "/mcp"


def get_mcp_server_settings() -> MCPServerSettings:
    return MCPServerSettings()
