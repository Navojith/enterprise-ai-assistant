"""Async MCP client: a long-lived `ClientSession` over Streamable HTTP, opened once in
`main.py`'s lifespan and reused for every call, with its own connect and per-call timeouts
(`docs/ARCHITECTURE.md`'s failure table distinguishes "MCP server down" from "tool timeout" —
these map to `MCPUnavailableError` and `ToolTimeoutError` respectively, never the same error).

A session per call was considered and rejected: establishing a Streamable HTTP session
(`initialize()`'s handshake) on every tool invocation would make every MCP-backed tool call pay
connection-setup latency on top of the call itself, on hardware where latency is already the
project's tightest budget (`docs/DECISIONS.md` §3). One session, opened at startup and closed at
shutdown, mirrors exactly how `main.py` already treats the Pinecone client and the checkpointer
pool — a process-lifetime singleton, not a per-request resource.

**A real bug found and fixed while testing `connect()` against an unreachable server:**
wrapping `self._stack.enter_async_context(streamable_http_client(...))` in `asyncio.wait_for`
raised `RuntimeError: Attempted to exit cancel scope in a different task than it was entered
in` the first time `aclose()` ran afterward — `asyncio.wait_for` wraps its argument in a new
child `Task` (so it can cancel it independently of the caller), so the transport's `anyio`
cancel scope is opened inside that short-lived child task; `AsyncExitStack.aclose()` later
exits it from *this* object's owning task instead, and `anyio` cancel scopes are strictly tied
to the task that opened them. The fix is not to add a `try`/`except` around the symptom, but
to never let `asyncio.wait_for` wrap an `enter_async_context` call whose matching exit happens
later, elsewhere, via the stack — `connect()` therefore only wraps `session.initialize()` (a
plain coroutine with no context-manager exit to later mismatch) in `wait_for`, and relies on
the transport's own connect timeout (30s by default, per `mcp.shared._httpx_utils.
create_mcp_http_client`'s documented default — the MCP SDK vendors its own HTTP stack as a
separate `httpx2` package, not this project's own pinned `httpx`) for the TCP-connect phase
itself. In practice this means `mcp_connect_timeout_seconds` bounds the initialize handshake
precisely, while an outright-unreachable server (connection refused, as in
`tests/tools/test_mcp_client.py`) still fails immediately — TCP refusal does not wait for any
timeout at all — and a server that accepts a connection but never responds is bounded by the
transport's own 30s default rather than this setting. Tightening that further would mean
constructing and passing a custom `httpx2.AsyncClient`, reaching past a dependency's declared
public surface for a low-priority requirement (`ASSESSMENT.md` calls the MCP tool "not a high
priority requirement") — not worth the coupling.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from backend.app.core.errors import MCPError, MCPUnavailableError

logger = structlog.get_logger(__name__)


class MCPClient:
    """Wraps one `ClientSession` connected over Streamable HTTP to `url`. Not itself an async
    context manager — `connect()`/`aclose()` are explicit because the object's lifetime is
    owned by `main.py`'s lifespan, not by a single `async with` block."""

    def __init__(
        self, url: str, *, connect_timeout_seconds: float, call_timeout_seconds: float
    ) -> None:
        self._url = url
        self._connect_timeout_seconds = connect_timeout_seconds
        self._call_timeout_seconds = call_timeout_seconds
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def connect(self) -> None:
        """Open the Streamable HTTP transport and the MCP session on top of it. Raises
        `MCPUnavailableError` — never a raw `httpx`/`anyio` exception — so `main.py`'s lifespan
        can degrade this to `mcp_client = None` the same way it already does for Pinecone.

        Only `session.initialize()` is wrapped in `asyncio.wait_for` — see this module's
        docstring for why wrapping `enter_async_context` itself is not safe here.
        """
        try:
            read_stream, write_stream = await self._stack.enter_async_context(
                streamable_http_client(self._url)
            )
            session = await self._stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await asyncio.wait_for(session.initialize(), timeout=self._connect_timeout_seconds)
        except Exception as exc:
            await self._stack.aclose()
            raise MCPUnavailableError(
                f"Could not connect to the MCP server at {self._url}: {exc}"
            ) from exc
        self._session = session
        logger.info("mcp_client_connected", url=self._url)

    async def aclose(self) -> None:
        await self._stack.aclose()
        self._session = None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call one MCP server tool and return its result — `structured_content` when the tool
        provided it (every tool `mcp_server/server.py` defines returns a JSON-serializable
        dict/list, so this is the common case), else the concatenation of any text content
        blocks. Raises `MCPError` if the server reports the call itself failed
        (`result.is_error`), and lets a timeout surface as whatever `asyncio.wait_for` around
        this call raises — `tools/registry.py::execute` is what turns that into
        `ToolTimeoutError` uniformly for every tool, MCP-backed or not.
        """
        if self._session is None:
            raise MCPUnavailableError("MCP client is not connected.")

        result = await self._session.call_tool(
            name, arguments, read_timeout_seconds=self._call_timeout_seconds
        )
        if result.is_error:
            raise MCPError(f"MCP tool {name!r} reported an error.", details={"tool": name})
        if result.structured_content is not None:
            return result.structured_content
        return "".join(block.text for block in result.content if isinstance(block, TextContent))
