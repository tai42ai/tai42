"""A low-level MCP server the fixtures launch over stdio to drive dispatch failures.

Unlike ``managed_mcp_server`` (a real FastMCP server that keeps its advertised output
schema and its actual return in sync), this server is deliberately built on the raw
``mcp.server.lowlevel.Server`` so it can misbehave in the exact shapes a downstream MCP
can inflict on the dispatch seam:

* ``ok`` — a well-behaved liveness tool, so a scenario proves the mount is live.
* ``bad_schema`` — advertises an object output schema requiring an integer ``count`` but
  returns a ``CallToolResult`` directly (bypassing the server's own output validation)
  whose ``structuredContent`` violates that schema. The calling MCP SDK validates the
  structured content against the advertised schema on the way back and raises.
* ``hang`` — sleeps far longer than the caller's per-call timeout, so the call times out.
* ``plain_text`` — advertises no output schema and returns a non-JSON text block, so the
  caller keeps it verbatim as a string (this shape is NOT a dispatch failure).

Launched as ``python -m tai42_e2e_fixtures.misbehaving_mcp_server`` — no network, no
package index, no ``uvx``.
"""

from __future__ import annotations

from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

_OK_TOOL = types.Tool(
    name="ok",
    description='Return the known object {"ok": true} — a liveness tool.',
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    outputSchema={
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    },
)

_BAD_SCHEMA_TOOL = types.Tool(
    name="bad_schema",
    description="Advertise an integer 'count' but return a string — an output-schema mismatch.",
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    outputSchema={
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    },
)

_HANG_TOOL = types.Tool(
    name="hang",
    description="Sleep past the caller's per-call timeout so the dispatch times out.",
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    outputSchema={
        "type": "object",
        "properties": {"done": {"type": "boolean"}},
        "required": ["done"],
        "additionalProperties": False,
    },
)

_PLAIN_TEXT_TOOL = types.Tool(
    name="plain_text",
    description="Return a non-JSON text block and advertise no output schema.",
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)

server: Server = Server("e2e-misbehaving-mcp")


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [_OK_TOOL, _BAD_SCHEMA_TOOL, _HANG_TOOL, _PLAIN_TEXT_TOOL]


@server.call_tool()
async def _call_tool(
    name: str, arguments: dict[str, Any]
) -> dict[str, Any] | list[types.ContentBlock] | types.CallToolResult:
    if name == "ok":
        return {"ok": True}
    if name == "bad_schema":
        # Return a fully-formed CallToolResult so the server skips its own output
        # validation and forwards structuredContent that violates the advertised
        # integer 'count' — the calling SDK is the one that must reject it.
        return types.CallToolResult(
            content=[types.TextContent(type="text", text='{"count": "not-an-integer"}')],
            structuredContent={"count": "not-an-integer"},
            isError=False,
        )
    if name == "hang":
        await anyio.sleep(3600)
        return {"done": True}
    if name == "plain_text":
        return [types.TextContent(type="text", text="this is not json { broken")]
    raise ValueError(f"unknown tool {name!r}")


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(notification_options=NotificationOptions()),
        )


if __name__ == "__main__":
    anyio.run(_run)
