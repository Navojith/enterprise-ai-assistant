"""Tests for `mcp_server/config.py` — the MCP server's own minimal settings, split out from
`backend/app/core/config.py::Settings` specifically so this process never needs to import
`backend.app` at all (`docs/ASSUMPTIONS_AND_TRADEOFFS.md` trade-off 25). Pins the defaults and
env-var names staying identical to what `Settings` still exposes for the client side, since the
whole point of the split is that a value in `.env` can't disagree between the two.
"""

from __future__ import annotations

from mcp_server.config import MCPServerSettings, get_mcp_server_settings


def test_defaults_load_without_an_env_file() -> None:
    settings = MCPServerSettings(_env_file=None)

    assert settings.mcp_server_host == "127.0.0.1"
    assert settings.mcp_server_port == 8100
    assert settings.mcp_server_path == "/mcp"


def test_reads_the_same_env_var_names_the_backend_settings_use() -> None:
    settings = MCPServerSettings(
        _env_file=None,
        MCP_SERVER_HOST="0.0.0.0",
        MCP_SERVER_PORT="9000",
        MCP_SERVER_PATH="/rpc",
    )

    assert settings.mcp_server_host == "0.0.0.0"
    assert settings.mcp_server_port == 9000
    assert settings.mcp_server_path == "/rpc"


def test_get_mcp_server_settings_returns_a_fresh_instance_each_call() -> None:
    """Not `@lru_cache`d like the backend's `get_settings()` — this is read once at process
    startup, never per-request, so there is no cache to keep coherent."""
    assert get_mcp_server_settings() is not get_mcp_server_settings()
