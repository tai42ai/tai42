"""MCP streamable-HTTP client transport over a Unix domain socket."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from tai42_kit.transport.base_uds_transport import BaseUDSTransport


class HTTPUDSTransport(BaseUDSTransport):
    """MCP transport speaking streamable-HTTP over a Unix domain socket."""

    @asynccontextmanager
    async def connect_session(self, **session_kwargs: Any) -> AsyncGenerator[ClientSession]:
        """Open an MCP client session over the socket, yielding it for the block."""
        client_cm = streamablehttp_client(
            url="http://127.0.0.1/mcp",
            httpx_client_factory=self._socket_factory,
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
        async with self._run_session(client_cm, session_kwargs) as session:
            yield session
