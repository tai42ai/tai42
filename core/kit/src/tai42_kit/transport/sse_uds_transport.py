from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client

from tai42_kit.transport.base_uds_transport import BaseUDSTransport


class SSEUDSTransport(BaseUDSTransport):
    @asynccontextmanager
    async def connect_session(self, **session_kwargs: Any) -> AsyncGenerator[ClientSession]:
        client_cm = sse_client(
            url="http://127.0.0.1/sse",
            httpx_client_factory=self._socket_factory,
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
        async with self._run_session(client_cm, session_kwargs) as session:
            yield session
