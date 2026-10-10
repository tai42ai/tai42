"""Start the event-stream shutdown watch of a serving loop from an empty context.

Every MCP streaming response is an ``EventSourceResponse``. To end its streams when the
server shuts down, the response library runs one long-lived watch task per serving
thread, started by the first response the thread serves and kept until shutdown. A task
keeps the context it was started in: started by a request, it would keep that request —
and through its ``scope["app"]`` the serving generation that admitted it — reachable for
the life of the loop, across every reload. The worker lifespan therefore serves one
response itself, from an empty context, before the worker admits any request; every
later response finds the watch already running.
"""

from __future__ import annotations

import asyncio
import contextvars

from sse_starlette import EventSourceResponse
from starlette.types import Message

__all__ = ["start_stream_shutdown_watch"]


class _NoEvents:
    """An event stream that sends no event and ends once its client is gone."""

    def __init__(self, client_gone: asyncio.Event) -> None:
        self._client_gone = client_gone

    def __aiter__(self) -> _NoEvents:
        return self

    async def __anext__(self) -> str:
        await self._client_gone.wait()
        raise StopAsyncIteration


async def _serve_one_response_to_a_departed_client() -> None:
    """Serve one event-stream response whose client disconnects the first time it is listened to."""
    client_gone = asyncio.Event()

    async def receive() -> Message:
        client_gone.set()
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        return None

    await EventSourceResponse(_NoEvents(client_gone))({"type": "http"}, receive, send)


async def start_stream_shutdown_watch() -> None:
    """Serve one event-stream response on the running loop from an empty context.

    Called by the worker lifespan once per serving loop, before the loop admits a request.
    """
    await asyncio.create_task(_serve_one_response_to_a_departed_client(), context=contextvars.Context())
