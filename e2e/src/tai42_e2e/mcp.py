"""Thin wrapper over ``fastmcp.Client`` for calling tools and listing them over
``POST /mcp`` (and the ``/app/<slug>`` sub-MCP mounts) against a running server.

The wrapper is transparently robust to D13a session termination. A config reload
swaps the serving epoch, which by design retires every live MCP session (the
swapped-in epoch serves a NEW session-id space; the old session manager is closed
on a deferred task once the driving call's result is delivered). A tool call or a
listing that lands on such a retired session gets a ``404`` the SDK surfaces as
``McpError`` "Session terminated" — either on the request send, or on the SDK's
post-call output-schema validation issuing a follow-up ``tools/list``. A real MCP
client handles this by RE-INITIALISING; :class:`McpClient` does the same
internally, so a test calls it plainly and still gets the call's REAL result
(``True``/``False``/error/dict) — no per-site retry wrap, and no assertion masked.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError
from mcp.types import Tool

from tai42_e2e.httpapi import _is_reloading as _response_is_reloading
from tai42_e2e.waiting import wait_for_async

# The substring the server stamps on a tool call it rejects while the reload gate is
# held (the ~2s boot-time self-resync, and any in-flight config reload). A boot-time
# call races that gate, so callers that fire a tool the instant a stack is ready poll
# past it with ``retry_on_reloading`` rather than failing on the transient rejection.
_RELOADING_MARKER = "reloading"

# The SDK's ``McpError`` message for a request that landed on a session the server
# already retired (a D13a epoch swap: the streamable-http transport answers ``404``,
# which ``mcp.client.streamable_http`` maps to a "Session terminated" JSONRPCError).
_SESSION_TERMINATED_MARKER = "Session terminated"

# Ceiling for transparently recovering from a D13a session swap: re-opening a fresh
# session (polling past a reload-gate handshake rejection on the fresh initialize) and
# re-issuing. A swap window settles in seconds; this is only the safety bound.
_SESSION_REINIT_DEADLINE = 15.0


def mcp_url(host: str, port: int, path: str = "/mcp") -> str:
    """The streamable-http MCP endpoint URL for a server (default ``/mcp``; sub-
    MCP apps mount under ``/app/<slug>``)."""
    return f"http://{host}:{port}{path}"


def _is_reloading(result: CallToolResult) -> bool:
    """Whether a tool result is the server's retriable reload-gate rejection."""
    return result.is_error and any(_RELOADING_MARKER in getattr(part, "text", "") for part in result.content)


def _is_session_terminated(exc: BaseException) -> bool:
    """Whether ``exc`` is the SDK's D13a session-swap rejection (a retired session)."""
    return isinstance(exc, McpError) and _SESSION_TERMINATED_MARKER in str(exc)


class McpClient:
    """A short-lived fastmcp client bound to one MCP endpoint. Use as an async
    context manager; each ``async with`` opens one streamable-http session.

    Session termination from a D13a epoch swap is handled transparently: the
    listing/tool-call surface re-initialises a fresh session and re-issues (bounded
    by :data:`_SESSION_REINIT_DEADLINE`), returning the call's real result."""

    def __init__(self, url: str, *, auth: str | None = None) -> None:
        self._url = url
        # ``auth`` is a raw bearer token (an ``sk-`` API key): fastmcp sends it as
        # ``Authorization: Bearer <token>`` on every request, so an access-controlled
        # ``/mcp`` endpoint authenticates the caller and the tool-edge authz check
        # runs against that principal's scopes. Retained so a session re-init after a
        # swap re-opens with the same credentials.
        self._auth = auth
        self._client = self._new_client()

    def _new_client(self) -> Client:
        return Client(self._url, auth=self._auth) if self._auth is not None else Client(self._url)

    @property
    def session_id(self) -> str | None:
        """The current streamable-http session id (the ``mcp-session-id`` the server minted
        for this session), or ``None`` before the initialize handshake completes. It CHANGES
        when a D13a epoch swap retires the session and this client transparently
        re-initialises a fresh one — the direct, observable signal of the session break."""
        return self._client.transport.get_session_id()

    async def __aenter__(self) -> McpClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._close()

    async def _close(self) -> None:
        """Close the underlying session, best-effort: a session the server already
        retired (D13a), or a torn transport, can raise on close — the session is gone
        regardless, so a close failure must not mask the caller's own outcome."""
        with contextlib.suppress(Exception):
            await self._client.__aexit__(None, None, None)

    async def _reinit(self) -> None:
        """Re-open a fresh session after the server retired the current one (D13a).

        Polls past a reload-gate rejection of the fresh ``initialize`` handshake (the
        swapped-in worker may still briefly hold its reload gate — the client raises
        ``HTTPStatusError`` before a session exists, so a tool call's own
        ``retry_on_reloading`` cannot cover it), bounded by the reinit deadline. Then
        warms the SDK's per-session output-schema cache with a ``list_tools`` so a
        re-run of a SELF-terminating reload op (whose own swap retires this fresh
        session too) issues no post-call follow-up ``tools/list`` on it — best-effort
        cache warming only, so a failure here never surfaces."""

        async def _reopened() -> bool | None:
            # A fresh Client per attempt so a half-open one never lingers; ``None`` on a
            # reload-gate handshake rejection tells the sanctioned poller to retry.
            await self._close()
            self._client = self._new_client()
            try:
                await self._client.__aenter__()
            except httpx.HTTPStatusError as exc:
                if not _response_is_reloading(exc.response):
                    raise
                return None
            return True

        await wait_for_async(
            _reopened, deadline=_SESSION_REINIT_DEADLINE, message="the MCP session re-init never left the reload gate"
        )
        with contextlib.suppress(Exception):
            await self._client.list_tools()

    async def _with_session_reinit[T](self, op: Callable[[], Awaitable[T]]) -> T:
        """Run ``op`` on the current session; if the server retired it mid-call (D13a),
        re-open a fresh session and re-run, bounded by the reinit deadline. Any OTHER
        error — and ``op``'s real return value — passes through untouched, so no test
        assertion (a falsy predicate, an ``is_error`` result) is ever masked."""
        deadline = time.monotonic() + _SESSION_REINIT_DEADLINE
        while True:
            try:
                return await op()
            except McpError as exc:
                if not _is_session_terminated(exc) or time.monotonic() >= deadline:
                    raise
                await self._reinit()

    async def list_tools(self) -> list[Tool]:
        # A lambda (not the bound ``self._client.list_tools``) so a retry after a
        # re-init re-reads the FRESH ``self._client``, never the retired one.
        return await self._with_session_reinit(lambda: self._client.list_tools())

    async def tool_names(self) -> list[str]:
        return [t.name for t in await self.list_tools()]

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
        non-reload error is returned/raised at once (never retried).

        A D13a session termination (the call's own reload, or a prior reload's
        deferred close, retiring this session) is handled underneath both paths: the
        session re-initialises and the call re-issues, so the caller sees the real
        result — the ``reloading`` retry and ``raise_on_error`` semantics are
        unchanged."""

        def _invoke(raise_err: bool) -> Awaitable[CallToolResult]:
            return self._with_session_reinit(
                lambda: self._client.call_tool(name, arguments or {}, raise_on_error=raise_err)
            )

        if not retry_on_reloading:
            return await _invoke(raise_on_error)

        async def ready() -> CallToolResult | None:
            result = await _invoke(False)
            return None if _is_reloading(result) else result

        result = await wait_for_async(
            ready, deadline=ready_deadline, message=f"tool {name!r} never left the reload gate"
        )
        if result.is_error and raise_on_error:
            text = next((getattr(part, "text", "") for part in result.content), "")
            raise ToolError(text or f"tool {name!r} returned an error result")
        return result
