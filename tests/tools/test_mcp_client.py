"""Tests for `tools/mcp_client.py`.

`call_tool`'s result-shape handling is tested against a fake `ClientSession` (the same
fake-the-boundary approach `tests/retrieval/test_hybrid.py` uses for Pinecone) — no real MCP
server involved. `connect()`'s failure path is tested against a real, fast connection attempt to
a port nothing is listening on, since that is exactly the failure this method exists to turn
into a typed `MCPUnavailableError` rather than a raw transport exception; the success path
(a real server round-trip) is exercised by `docs/SETUP.md`'s manual run, not by this suite —
matching how `test_hybrid.py` treats live Pinecone.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent

from backend.app.core.errors import MCPError, MCPUnavailableError
from backend.app.tools.mcp_client import MCPClient


def _connected_client(session: AsyncMock) -> MCPClient:
    client = MCPClient(
        "http://127.0.0.1:1/mcp", connect_timeout_seconds=1.0, call_timeout_seconds=1.0
    )
    client._session = session  # bypass connect(): unit-testing call_tool in isolation
    return client


class TestConnect:
    async def test_connecting_to_an_unreachable_server_raises_mcp_unavailable(self) -> None:
        # Port 1 is a reserved, unassignable TCP port — nothing can ever be listening there, so
        # this connection attempt fails fast and deterministically rather than timing out.
        client = MCPClient(
            "http://127.0.0.1:1/mcp", connect_timeout_seconds=1.0, call_timeout_seconds=1.0
        )

        with pytest.raises(MCPUnavailableError):
            await client.connect()


class TestCallTool:
    async def test_returns_structured_content_when_present(self) -> None:
        session = AsyncMock()
        session.call_tool.return_value = SimpleNamespace(
            is_error=False, structured_content={"a": 1}, content=[]
        )
        client = _connected_client(session)

        result = await client.call_tool("some_tool", {})

        assert result == {"a": 1}

    async def test_falls_back_to_text_content_when_unstructured(self) -> None:
        session = AsyncMock()
        text_block = SimpleNamespace(text="hello")
        session.call_tool.return_value = SimpleNamespace(
            is_error=False, structured_content=None, content=[text_block]
        )
        client = _connected_client(session)

        # `TextContent` is a real pydantic model in `mcp.types`; a plain `SimpleNamespace` isn't
        # one, so `isinstance` correctly excludes it — this asserts the fallback path returns
        # nothing rather than misreading an arbitrary object as text, and is intentionally
        # exercised again below with a real `TextContent`.
        result = await client.call_tool("some_tool", {})
        assert result == ""

    async def test_real_text_content_blocks_are_concatenated(self) -> None:
        session = AsyncMock()
        session.call_tool.return_value = SimpleNamespace(
            is_error=False,
            structured_content=None,
            content=[
                TextContent(type="text", text="hello "),
                TextContent(type="text", text="world"),
            ],
        )
        client = _connected_client(session)

        result = await client.call_tool("some_tool", {})

        assert result == "hello world"

    async def test_a_server_reported_error_raises_mcp_error(self) -> None:
        session = AsyncMock()
        session.call_tool.return_value = SimpleNamespace(
            is_error=True, structured_content=None, content=[]
        )
        client = _connected_client(session)

        with pytest.raises(MCPError):
            await client.call_tool("some_tool", {})

    async def test_calling_before_connect_raises_mcp_unavailable(self) -> None:
        client = MCPClient(
            "http://127.0.0.1:1/mcp", connect_timeout_seconds=1.0, call_timeout_seconds=1.0
        )

        with pytest.raises(MCPUnavailableError):
            await client.call_tool("some_tool", {})
