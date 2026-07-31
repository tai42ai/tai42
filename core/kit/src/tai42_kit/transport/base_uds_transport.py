import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any

import httpx
from fastmcp.client.transports import ClientTransport
from mcp import ClientSession

logger = logging.getLogger(__name__)


class SafeAsyncClient(httpx.AsyncClient):
    """Prevents 'InvalidStateError' during UDS shutdown race conditions."""

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        try:
            await super().__aexit__(exc_type, exc_value, traceback)
        except asyncio.exceptions.InvalidStateError:
            # Benign UDS shutdown race; surface at debug, never propagate.
            logger.debug("Ignored InvalidStateError during UDS client shutdown", exc_info=True)


class BaseUDSTransport(ClientTransport):
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    def _socket_factory(
        self,
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        # Build a fresh UDS transport per client so each client owns and closes
        # its own — a shared transport would be closed by the first client to
        # exit, breaking every other client still using it.
        uds_transport = httpx.AsyncHTTPTransport(
            uds=self.socket_path,
            retries=0,
            http1=True,
            http2=False,
        )
        return SafeAsyncClient(
            follow_redirects=True,
            transport=uds_transport,
            base_url="http://127.0.0.1",
            headers=headers,
            auth=auth,
            timeout=timeout if timeout is not None else httpx.Timeout(60.0),
        )

    @asynccontextmanager
    async def _run_session(
        self,
        client_cm: Any,
        session_kwargs: dict[str, Any],
    ) -> AsyncGenerator[ClientSession]:
        """Common logic to wrap mcp client streams into a ClientSession."""
        async with client_cm as streams:
            # streamable-http returns (read, write, handle), sse returns (read, write)
            read, write = streams[0], streams[1]
            # Forward every session kwarg (read_timeout_seconds, the callbacks,
            # client_info, …) — never silently drop the caller's options.
            async with ClientSession(read, write, **session_kwargs) as session:
                yield session
