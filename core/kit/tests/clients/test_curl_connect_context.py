"""The kit's pooled curl session keeps no context of the caller whose request opened a connection.

A keep-alive connection outlives the request that opened it, and the loop callbacks libcurl
arms for it copy the context they are created in. Each case sends a request from a caller
whose context holds a marker object and checks the marker is collected once the caller is
gone while the session, and its keep-alive connection, stay pooled. The server is an
in-process keep-alive HTTP stub. Every case runs on asyncio and, where it exists, uvloop.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import sys
import weakref
from collections.abc import Callable

import pytest

pytest.importorskip("curl_cffi")

from tai42_kit.clients.impl.curl import CurlClient

_caller: contextvars.ContextVar[object] = contextvars.ContextVar("caller")

_LOOP_FACTORIES: list[tuple[str, Callable[[], asyncio.AbstractEventLoop]]] = [("asyncio", asyncio.new_event_loop)]
if sys.platform != "win32":
    import uvloop

    _LOOP_FACTORIES.append(("uvloop", uvloop.new_event_loop))


class _Marker:
    """An object only the caller's context holds."""


async def _keep_alive_answerer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Answer every request on the connection with ``200 ok`` and keep the connection open."""
    try:
        while True:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                head += chunk
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok")
            await writer.drain()
    finally:
        writer.close()


async def _marker_alive_after_requests(requests: int) -> tuple[bool, int]:
    """Send ``requests`` requests from a caller whose context holds a marker.

    Returns whether the marker is still alive while the session stays pooled, and how many
    connections the server accepted.
    """
    connections = 0

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        await _keep_alive_answerer(reader, writer)

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
    client = CurlClient()
    try:

        async def caller() -> weakref.ref[_Marker]:
            marker = _Marker()
            _caller.set(marker)
            for _ in range(requests):
                async with client.current(session_params={}) as session:
                    response = await session.get(url)
                    assert response.status_code == 200
            return weakref.ref(marker)

        marker = await asyncio.create_task(caller())
        # Let libcurl's timer and the session's own timeout checker run with the connection idle.
        await asyncio.sleep(0.3)
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()
        return marker() is not None, connections
    finally:
        await client.close(session_params={})
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("loop_factory", [f for _, f in _LOOP_FACTORIES], ids=[n for n, _ in _LOOP_FACTORIES])
def test_a_pooled_session_connection_keeps_no_context_of_the_caller_that_opened_it(
    loop_factory: Callable[[], asyncio.AbstractEventLoop],
) -> None:
    alive, connections = asyncio.run(_marker_alive_after_requests(1), loop_factory=loop_factory)

    assert connections == 1
    assert alive is False


@pytest.mark.parametrize("loop_factory", [f for _, f in _LOOP_FACTORIES], ids=[n for n, _ in _LOOP_FACTORIES])
def test_a_reused_keep_alive_connection_keeps_no_context_of_its_caller(
    loop_factory: Callable[[], asyncio.AbstractEventLoop],
) -> None:
    alive, connections = asyncio.run(_marker_alive_after_requests(3), loop_factory=loop_factory)

    assert connections == 1  # every request rode the one pooled keep-alive connection
    assert alive is False
