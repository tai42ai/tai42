import asyncio
from typing import Literal

import anyio
from fastmcp import Client
from tai42_contract.manifest import TaiMCPConfig

from tai42_kit.clients.base import PooledClient, is_loop_bound_runtime_error, reject_unknown_connection_kwargs
from tai42_kit.clients.settings import mcp_client_settings
from tai42_kit.transport import get_mcp_transport

# Connection kwargs a FastMCP client accepts. Anything else is rejected so it
# can't silently split the pool key.
_ALLOWED_KWARGS = frozenset({"config", "uds_protocol"})


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
        timeout = mcp_client_settings().connect_timeout_seconds
        try:
            return await asyncio.wait_for(Client(transport).__aenter__(), timeout=timeout)
        except TimeoutError as exc:
            raise TimeoutError(
                f"MCP client connect/init did not complete within {timeout}s (MCP_CLIENT_CONNECT_TIMEOUT_SECONDS)"
            ) from exc

    async def _close(self, client: Client):
        await client.__aexit__(None, None, None)

    def _disconnection_exceptions(self) -> tuple[type[Exception], ...]:
        return anyio.ClosedResourceError, anyio.BrokenResourceError

    def _is_disconnection_error(self, exc: BaseException) -> bool:
        # anyio/asyncio surface a dead loop-bound transport as a plain
        # RuntimeError ("Event loop is closed" / "... attached to a different
        # loop"); match those by message so an unrelated RuntimeError raised by
        # caller code never tears down the pool.
        return super()._is_disconnection_error(exc) or is_loop_bound_runtime_error(exc)
