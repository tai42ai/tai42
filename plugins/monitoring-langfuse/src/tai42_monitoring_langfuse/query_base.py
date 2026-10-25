"""The shared base for the Langfuse query objects: handle, off-loop client access, and the caps."""

from __future__ import annotations

import asyncio

import httpx

from tai42_monitoring_langfuse.client_manager import LangfuseClientManager

# The largest page one ``list_traces`` call serves.
PAGE_SIZE = 100
# The most rows one observations list call returns.
WALK_LIMIT = 1000
# The trace ids one ``traceId any of`` clause carries; the request URL bounds the list.
TRACE_ID_CHUNK = 100
# The furthest run a newest-first list pages to.
NATIVE_MAX_REACH = 5000
# The most runs a ranked top-N query returns.
RANKED_MAX_REACH = 1000
# The most runs a list reads in its time range to rank or filter them in the plugin.
WINDOW_MAX_ROOTS = 2000
# The most observations a list reads to total cost and tokens and find levels and models.
ENRICH_MAX_OBSERVATIONS = 50_000


class _LangfuseQuery:
    """Holds the client manager every query surface shares."""

    def __init__(self, manager: LangfuseClientManager) -> None:
        self._m = manager

    async def _active_client(self) -> httpx.Client:
        # First use constructs the client — off the event loop.
        return await asyncio.to_thread(self._m.active_client)
