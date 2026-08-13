import asyncio
from typing import Literal

import anyio
import httpx
from fastmcp import Client
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED
from tai42_contract.errors import ClientConnectError
from tai42_contract.manifest import TaiMCPConfig

from tai42_kit.clients.base import PooledClient, is_loop_bound_runtime_error, reject_unknown_connection_kwargs
from tai42_kit.clients.settings import mcp_client_settings
from tai42_kit.transport import get_mcp_transport

# Connection kwargs a FastMCP client accepts. Anything else is rejected so it
# can't silently split the pool key.
_ALLOWED_KWARGS = frozenset({"config", "uds_protocol"})

# fastmcp collapses a dead background session into a plain RuntimeError carrying
# one of these messages; matched by substring so an unrelated caller RuntimeError
# never evicts the pooled client.
_DEAD_SESSION_ERROR_MARKERS = (
    "Client is not connected",
    "Server session was closed unexpectedly",
    "Session task completed unexpectedly",
)

# The streamable-http MCP SDK answers a POST carrying a stale session id (the
# upstream restarted and dropped it) with HTTP 404 and surfaces it as an McpError
# of this exact shape. 32600 is the SDK's literal code (a positive quirk, not the
# negative JSON-RPC INVALID_REQUEST); paired with the message text so an unrelated
# error on a nearby code cannot false-evict the pooled client.
_SESSION_TERMINATED_CODE = 32600
_SESSION_TERMINATED_MESSAGE = "Session terminated"


class FastMCPClient(PooledClient[Client]):
    async def _create(self, **kwargs) -> Client:
        if "config" not in kwargs:
            raise KeyError("FastMCP client requires a `config` kwarg")

        config = kwargs["config"]
        if not config or not isinstance(config, dict):
            raise TypeError("FastMCP client `config` must be a non-empty dict")
        reject_unknown_connection_kwargs("FastMCP client", kwargs, _ALLOWED_KWARGS)

        # The MCP wire protocol over a UDS socket; the caller (which owns the
        # runtime/CLI) supplies it. Ignored for non-UDS servers.
        uds_protocol: Literal["http", "sse"] = kwargs.get("uds_protocol", "http")
        transport = get_mcp_transport(TaiMCPConfig(**config), uds_protocol=uds_protocol)
        # No ambient-header forwarding: fastmcp transports default
        # ``forward_incoming_headers=False``, so this server's own inbound headers
        # (incl. auth) are never leaked to the downstream MCP — only the headers
        # declared in ``config`` are sent. The conformance tests lock this so a
        # future fastmcp default-change fails loudly instead of leaking silently.
        #
        # Bound the connect/init: this runs inside the pooled per-key creation
        # lock, so an unbounded connect-stall would queue every same-config caller
        # forever. wait_for cancels the inner enter on expiry, letting fastmcp
        # unwind its partial transport state; nothing is registered in the pool.
        # Any build failure — the fastmcp connect (__aenter__ raises RuntimeError
        # on a down upstream) or the connect timeout — is wrapped in
        # ClientConnectError so the dispatch seam's unavailable-client handling
        # catches it; CancelledError is a BaseException and propagates untouched.
        timeout = mcp_client_settings().connect_timeout_seconds
        try:
            return await asyncio.wait_for(Client(transport).__aenter__(), timeout=timeout)
        except TimeoutError as exc:
            raise ClientConnectError(
                "MCP client connect failed: connect/init did not complete within "
                f"{timeout}s (MCP_CLIENT_CONNECT_TIMEOUT_SECONDS)"
            ) from exc
        except Exception as exc:
            raise ClientConnectError(f"MCP client connect failed: {exc}") from exc

    async def _close(self, client: Client):
        await client.__aexit__(None, None, None)

    def _disconnection_exceptions(self) -> tuple[type[BaseException], ...]:
        return (*super()._disconnection_exceptions(), anyio.ClosedResourceError, anyio.BrokenResourceError)

    def _is_disconnection_error(self, exc: BaseException) -> bool:
        # anyio/asyncio surface a dead loop-bound transport as a plain
        # RuntimeError ("Event loop is closed" / "... attached to a different
        # loop"); match those by message so an unrelated RuntimeError raised by
        # caller code never tears down the pool.
        if super()._is_disconnection_error(exc) or is_loop_bound_runtime_error(exc):
            return True
        # A transport-level httpx failure means the connection is dead — except a
        # timeout, which means slow, not dead: evicting on it would churn the pool
        # into reconnects under load.
        if isinstance(exc, httpx.TransportError) and not isinstance(exc, httpx.TimeoutException):
            return True
        # An MCP protocol error carrying the connection-closed code, or the stale
        # session-terminated shape after an upstream restart, is a dead session;
        # other codes (tool errors, request timeouts) are caller-visible and must
        # not evict.
        if isinstance(exc, McpError):
            if exc.error.code == CONNECTION_CLOSED:
                return True
            if exc.error.code == _SESSION_TERMINATED_CODE and exc.error.message == _SESSION_TERMINATED_MESSAGE:
                return True
        # fastmcp re-raises a collapsed session task as a plain RuntimeError with
        # one of these messages; match by substring like the loop-bound markers so
        # an unrelated caller RuntimeError never tears down the pool.
        return isinstance(exc, RuntimeError) and any(marker in str(exc) for marker in _DEAD_SESSION_ERROR_MARKERS)
