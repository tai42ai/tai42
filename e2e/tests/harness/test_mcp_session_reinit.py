"""Harness self-test for the MCP client's recovery from a retired session: a call whose reply
was delivered is never sent again, and a call the retired session rejected is."""

from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.types import ASGIApp, Receive, Scope, Send

from tai42_e2e import ports, wait_for_async
from tai42_e2e.mcp import McpClient

# Pure harness self-test: it drives a stand-in MCP server, never a stack of the product.
pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs("no-stack"),
    # A session's SSE response the stand-in still holds when it stops leaves the SDK's reader
    # stream unclosed (``sse_starlette`` iterates it to its end); anyio reports it when freed.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]

_SESSION_HEADER = b"mcp-session-id"


@dataclass
class _Retiring:
    """The stand-in's record: the sessions it retired and each run of its one tool."""

    retired: set[str] = field(default_factory=set)
    runs: list[str] = field(default_factory=list)


class _RetiredSessions:
    """Answers ``404`` to every request on a retired session, as a server that swapped its sessions out does."""

    def __init__(self, app: ASGIApp, record: _Retiring) -> None:
        self._app = app
        self._record = record

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            session = dict(scope["headers"]).get(_SESSION_HEADER, b"").decode()
            if session in self._record.retired:
                await send({"type": "http.response.start", "status": 404, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await self._app(scope, receive, send)


@asynccontextmanager
async def _mcp_server_that_retires_the_calling_session(record: _Retiring) -> AsyncIterator[str]:
    """A streamable-http MCP server whose one tool retires the session that called it, then answers."""
    server = FastMCP("stand-in")

    @server.tool
    async def retire_my_session(label: str) -> str:
        record.runs.append(label)
        record.retired.add(get_http_request().headers["mcp-session-id"])
        return f"ran {label}"

    app = server.http_app(path="/mcp")
    port = ports.allocate_port()
    uv = uvicorn.Server(
        uvicorn.Config(_RetiredSessions(app, record), host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    serving = asyncio.create_task(uv.serve())
    try:
        async with app.router.lifespan_context(app):

            async def _started() -> bool:
                return uv.started

            await wait_for_async(_started, deadline=10.0, message="the stand-in MCP server never started")
            yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uv.should_exit = True
        await serving
        ports.release_port(port)
        # Free the server's leftover response streams inside the test, where the filter above applies.
        gc.collect()


async def test_a_call_whose_reply_was_delivered_is_not_sent_again_when_its_session_is_then_retired() -> None:
    record = _Retiring()
    async with _mcp_server_that_retires_the_calling_session(record) as url, McpClient(url, read_timeout=10.0) as mcp:
        result = await asyncio.wait_for(mcp.call_tool("retire_my_session", {"label": "once"}), timeout=30.0)

    assert result.data == "ran once"
    assert record.runs == ["once"]


async def test_a_call_a_retired_session_rejected_is_sent_on_a_fresh_session() -> None:
    record = _Retiring()
    async with _mcp_server_that_retires_the_calling_session(record) as url, McpClient(url, read_timeout=10.0) as mcp:
        await mcp.call_tool("retire_my_session", {"label": "first"})
        second = await asyncio.wait_for(mcp.call_tool("retire_my_session", {"label": "second"}), timeout=30.0)

    assert second.data == "ran second"
    assert record.runs == ["first", "second"]
