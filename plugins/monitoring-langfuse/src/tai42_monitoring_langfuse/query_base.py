"""The shared base for the Langfuse query objects: the client-manager handle,
request options, and off-loop client access."""

from __future__ import annotations

import asyncio
from typing import Any

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager

_PAGE_SIZE = 100


class _LangfuseQuery:
    """Holds the client manager and the request plumbing every query surface shares."""

    def __init__(self, manager: LangfuseClientManager) -> None:
        self._m = manager

    def _request_options(self) -> dict[str, Any]:
        return {"timeout_in_seconds": self._m.read_timeout_seconds()}

    async def _active_client(self) -> Any:
        # First use may construct the SDK clients — off the event loop.
        return await asyncio.to_thread(self._m.active_client)
