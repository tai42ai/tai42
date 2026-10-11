"""Harness self-test for the MCP client's read bound: how the stack derives it, and that a
reply the server never delivers fails as the SDK's named request timeout."""

from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import uvicorn
from fastmcp import FastMCP
from mcp.shared.exceptions import McpError

from tai42_e2e import ports, wait_for_async
from tai42_e2e.mcp import McpClient, mcp_read_timeout, platform_apply_timeout

# Pure harness self-test: it drives a stand-in MCP server, never a stack of the product.
pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs("no-stack"),
    # The stand-in server stops while the never-answered call's SSE response is still open; the
    # SDK's reader stream of that response is never closed (``sse_starlette`` iterates it to its
    # end) and anyio reports it when freed. A third-party resource report, matched narrowly.
    pytest.mark.filterwarnings("ignore:Unclosed <MemoryObjectReceiveStream:ResourceWarning"),
]


def test_the_read_bound_is_the_stacks_apply_timeout_plus_the_boot_bound() -> None:
    assert mcp_read_timeout({"TAI_BUS_APPLY_TIMEOUT": "5"}, 120.0) == 125.0
    assert mcp_read_timeout({}, 120.0) == platform_apply_timeout() + 120.0


def test_the_platform_apply_timeout_is_the_bus_settings_default() -> None:
    assert platform_apply_timeout() == 30.0


@asynccontextmanager
async def _mcp_server_that_never_answers() -> AsyncIterator[str]:
    """A streamable-http MCP server whose one tool never returns its reply."""
    server = FastMCP("stand-in")

    @server.tool
    async def never_answers() -> str:
        await asyncio.Event().wait()
        return "unreachable"

    port = ports.allocate_port()
    uv = uvicorn.Server(uvicorn.Config(server.http_app(path="/mcp"), host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(uv.serve())
    try:

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


async def test_a_reply_never_delivered_fails_as_the_named_request_timeout() -> None:
    async with _mcp_server_that_never_answers() as url, McpClient(url, read_timeout=0.5) as mcp:
        with pytest.raises(McpError, match="Timed out while waiting for response to ClientRequest"):
            await asyncio.wait_for(mcp.call_tool("never_answers"), timeout=30.0)
