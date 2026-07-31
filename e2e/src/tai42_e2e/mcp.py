"""Thin wrapper over ``fastmcp.Client`` for calling tools and listing them over
``POST /mcp`` (and the ``/app/<slug>`` sub-MCP mounts) against a running server."""

from __future__ import annotations

from typing import Any

from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError
from mcp.types import Tool

from tai42_e2e.waiting import wait_for_async

# The substring the server stamps on a tool call it rejects while the reload gate is
# held (the ~2s boot-time self-resync, and any in-flight config reload). A boot-time
# call races that gate, so callers that fire a tool the instant a stack is ready poll
# past it with ``retry_on_reloading`` rather than failing on the transient rejection.
_RELOADING_MARKER = "reloading"


def mcp_url(host: str, port: int, path: str = "/mcp") -> str:
    """The streamable-http MCP endpoint URL for a server (default ``/mcp``; sub-
    MCP apps mount under ``/app/<slug>``)."""
    return f"http://{host}:{port}{path}"


def _is_reloading(result: CallToolResult) -> bool:
    """Whether a tool result is the server's retriable reload-gate rejection."""
    return result.is_error and any(_RELOADING_MARKER in getattr(part, "text", "") for part in result.content)


class McpClient:
    """A short-lived fastmcp client bound to one MCP endpoint. Use as an async
    context manager; each ``async with`` opens one streamable-http session."""

    def __init__(self, url: str, *, auth: str | None = None) -> None:
        self._url = url
        # ``auth`` is a raw bearer token (an ``sk-`` API key): fastmcp sends it as
        # ``Authorization: Bearer <token>`` on every request, so an access-controlled
        # ``/mcp`` endpoint authenticates the caller and the tool-edge authz check
        # runs against that principal's scopes.
        self._client = Client(url, auth=auth) if auth is not None else Client(url)

    async def __aenter__(self) -> McpClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.__aexit__(*exc)

    async def list_tools(self) -> list[Tool]:
        return await self._client.list_tools()

    async def tool_names(self) -> list[str]:
        return [t.name for t in await self._client.list_tools()]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        raise_on_error: bool = True,
        retry_on_reloading: bool = False,
        ready_deadline: float = 10.0,
    ) -> CallToolResult:
        """Call ``name`` with ``arguments``. With ``raise_on_error=False`` an MCP
        error result is returned (``result.is_error``) rather than raised — for
        tests that assert the error path (``e2e_fail``).

        With ``retry_on_reloading=True`` a call that hits the boot-time reload gate
        (the ~2s self-resync) is polled past on the sanctioned :func:`wait_for_async`
        interval until the worker leaves the gate or ``ready_deadline`` elapses — the
        retry every real client applies to the retriable ``reloading`` rejection. A
        non-reload error is returned/raised at once (never retried)."""
        if not retry_on_reloading:
            return await self._client.call_tool(name, arguments or {}, raise_on_error=raise_on_error)

        async def ready() -> CallToolResult | None:
            result = await self._client.call_tool(name, arguments or {}, raise_on_error=False)
            return None if _is_reloading(result) else result

        result = await wait_for_async(
            ready, deadline=ready_deadline, message=f"tool {name!r} never left the reload gate"
        )
        if result.is_error and raise_on_error:
            text = next((getattr(part, "text", "") for part in result.content), "")
            raise ToolError(text or f"tool {name!r} returned an error result")
        return result
