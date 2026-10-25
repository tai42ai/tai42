"""The event-stream shutdown watch start.

The worker lifespan serves one event-stream response itself before admitting a request,
so the response library's per-loop shutdown watch starts from an empty context. That one
response must complete on the installed response library: a failure here fails the
worker lifespan and the server never becomes ready.
"""

from __future__ import annotations

import asyncio

from tai42_skeleton.app.stream_shutdown_watch import start_stream_shutdown_watch


async def test_the_watch_start_serves_its_one_response_and_returns() -> None:
    await asyncio.wait_for(start_stream_shutdown_watch(), timeout=10)
