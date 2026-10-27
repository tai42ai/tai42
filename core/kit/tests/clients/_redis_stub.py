"""Loopback endpoints the Redis socket tests dial: an in-process Redis stub and a closed port."""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

# The error a handshake read raises when the server closes the fresh socket unanswered.
HANDSHAKE_DROP = "Connection closed by server."


def closed_loopback_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class Stub:
    """A loopback Redis stub: answers HELLO, PING, SUBSCRIBE and anything else; can drop and come back.

    A connection that says ``HELLO 3`` is answered in RESP3, its pub/sub replies as pushes;
    any other is answered in RESP2, a ``PING`` on a subscribed one with a ``pong`` message
    carrying its argument, as Redis answers a pub/sub health check. The stub can also drop
    a connection's handshake (:meth:`drop_next`) and publish to every subscribed connection
    (:meth:`publish`).
    """

    def __init__(self, *, drop_on_ping: int | None = None) -> None:
        self.port = 0
        self.servers: list[asyncio.Server] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.subscribers: list[asyncio.StreamWriter] = []
        self.resp3: set[asyncio.StreamWriter] = set()
        self.subscribes = 0
        self.pings = 0
        self.drop_on_ping = drop_on_ping
        self.handshake_drops = 0

    def drop_next(self, connections: int) -> None:
        """Close the next ``connections`` accepted connections before any reply; answer normally after."""
        self.handshake_drops = connections

    def publish(self, channel: bytes, data: bytes) -> None:
        """Send a pub/sub message on ``channel`` to every connection that subscribed."""
        message = b"3\r\n$7\r\nmessage\r\n$%d\r\n%s\r\n$%d\r\n%s\r\n" % (len(channel), channel, len(data), data)
        for writer in self.subscribers:
            if not writer.is_closing():
                writer.write(self._push_head(writer) + message)

    def _push_head(self, writer: asyncio.StreamWriter) -> bytes:
        return b">" if writer in self.resp3 else b"*"

    def _pong(self, writer: asyncio.StreamWriter, words: list[bytes]) -> bytes:
        """Redis's reply to ``PING [message]``: a ``pong`` message on a subscribed RESP2 connection."""
        if writer in self.subscribers and writer not in self.resp3:
            message = words[0] if words else b""
            return b"*2\r\n$4\r\npong\r\n$%d\r\n%s\r\n" % (len(message), message)
        if words:
            return b"$%d\r\n%s\r\n" % (len(words[0]), words[0])
        return b"+PONG\r\n"

    @staticmethod
    async def _read_command(reader: asyncio.StreamReader) -> list[bytes] | None:
        header = await reader.readline()
        if not header:
            return None
        words = []
        for _ in range(int(header[1:])):
            length = int((await reader.readline())[1:])
            words.append((await reader.readexactly(length + 2))[:-2])
        return words

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.handshake_drops:
            self.handshake_drops -= 1
            # The handshake's first command is read in full, so the close is a FIN the
            # client reads as the server closing, never a reset of unread data.
            with contextlib.suppress(ConnectionError, asyncio.IncompleteReadError):
                await self._read_command(reader)
            writer.close()
            return
        self.writers.append(writer)
        try:
            while words := await self._read_command(reader):
                command = words[0].upper()
                if command == b"HELLO" and words[1:2] == [b"3"]:
                    self.resp3.add(writer)
                    writer.write(b"%1\r\n$5\r\nproto\r\n:3\r\n")
                elif command == b"PING":
                    self.pings += 1
                    if self.pings == self.drop_on_ping:
                        self.drop()
                        return
                    writer.write(self._pong(writer, words[1:]))
                elif command == b"SUBSCRIBE":
                    self.subscribes += 1
                    self.subscribers.append(writer)
                    for count, channel in enumerate(words[1:], 1):
                        reply = b"3\r\n$9\r\nsubscribe\r\n$%d\r\n%s\r\n:%d\r\n" % (len(channel), channel, count)
                        writer.write(self._push_head(writer) + reply)
                else:
                    writer.write(b"+OK\r\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def start(self) -> None:
        server = await asyncio.start_server(self.handle, "127.0.0.1", self.port)
        self.port = server.sockets[0].getsockname()[1]
        self.servers.append(server)

    def drop(self) -> None:
        """Stop listening and abort every open connection."""
        for server in self.servers:
            server.close()
        for writer in self.writers:
            writer.transport.abort()
        self.writers.clear()

    async def drop_closed(self) -> None:
        """Drop, then wait until every aborted connection's socket is closed.

        ``abort`` closes a socket on the loop's next pass; a client that sends
        before then reaches the old socket, and its send succeeds.
        """
        writers = list(self.writers)
        self.drop()
        for writer in writers:
            await writer.wait_closed()

    def come_back(self, loop: asyncio.AbstractEventLoop) -> None:
        """Listen again on the same port at once; the stub starts answering on the loop."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        sock.listen()

        async def serve() -> None:
            self.servers.append(await asyncio.start_server(self.handle, sock=sock))

        loop.call_soon_threadsafe(lambda: loop.create_task(serve()))

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"


@asynccontextmanager
async def serving_stub(**kwargs: Any) -> AsyncIterator[Stub]:
    stub = Stub(**kwargs)
    await stub.start()
    try:
        yield stub
    finally:
        stub.drop()
