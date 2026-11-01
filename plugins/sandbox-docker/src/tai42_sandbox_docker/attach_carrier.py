"""The connection under an exec attachment: bytes in and out, backpressure, and the stream's end.

An :class:`AttachCarrier` is the :class:`asyncio.Protocol` on the raw engine connection an
exec's stream rides. It hands every byte the engine sends to its :class:`StreamReceiver`,
writes the attachment's bytes with backpressure from the raw transport, ends the stream
once (the engine's end of it, a lost connection, or a close or an abort here, each aborting
the raw transport so stdin the command never took is dropped), and ends stdin through one
method, :meth:`AttachCarrier.end_input`. :class:`PlainCarrier` carries the stream as it
is, over a unix socket or plain TCP; the TLS carrier
(:class:`~tai42_sandbox_docker.attach_tls.TlsCarrier`) carries it over the engine's mTLS.
"""

from __future__ import annotations

import abc
import asyncio
from typing import Protocol, cast


class StreamReceiver(Protocol):
    """What an attachment takes from its carrier: the engine's bytes, then the stream's end."""

    def bytes_received(self, data: bytes) -> None:
        """Take ``data`` the engine sent."""
        ...

    def stream_ended(self, error: Exception | None) -> None:
        """Take the stream's end, with the ``error`` that ended it, or ``None`` for a clean end."""
        ...


class AttachCarrier(asyncio.Protocol, abc.ABC):
    """The raw engine connection of one exec attachment; see the module docstring."""

    def __init__(self, receiver: StreamReceiver) -> None:
        """Hand everything the engine sends to ``receiver``."""
        self._receiver = receiver
        self._transport: asyncio.Transport | None = None
        self._write_paused = False
        self._drain_waiter: asyncio.Future[None] | None = None
        self._ended = False

    @property
    def transport(self) -> asyncio.Transport:
        """The raw transport the stream rides, once connected."""
        if self._transport is None:
            raise RuntimeError("the attach carrier is not connected")
        return self._transport

    @property
    def ended(self) -> bool:
        """Whether the stream has ended: no byte goes either way any more."""
        return self._ended

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Keep the raw ``transport`` the stream rides."""
        self._transport = cast(asyncio.Transport, transport)

    def connection_lost(self, exc: Exception | None) -> None:
        """End the stream: the connection is gone, with ``exc`` when it was lost under an error."""
        self._end(exc)

    def eof_received(self) -> bool:
        """End the stream at the engine's end of the raw connection."""
        self._end(None)
        return False

    def pause_writing(self) -> None:
        """Hold the writer: the raw transport's buffer is full."""
        self._write_paused = True

    def resume_writing(self) -> None:
        """Release the writer: the raw transport's buffer drained."""
        self._write_paused = False
        self._wake_drain()

    async def start(self) -> None:
        """Make the connection ready to carry the stream; a plain connection already is."""

    async def drain(self) -> None:
        """Wait until the raw transport takes more writes, or the stream ends."""
        while self._write_paused and not self._ended:
            self._drain_waiter = asyncio.get_running_loop().create_future()
            try:
                await self._drain_waiter
            finally:
                self._drain_waiter = None

    def pause_reading(self) -> None:
        """Stop reading from the engine while the receiver holds enough unread output."""
        if not self._ended:
            self.transport.pause_reading()

    def resume_reading(self) -> None:
        """Read from the engine again."""
        if not self._ended:
            self.transport.resume_reading()

    @abc.abstractmethod
    def write(self, data: bytes) -> None:
        """Send ``data`` to the engine."""

    @abc.abstractmethod
    def end_input(self) -> None:
        """End the stream's write direction, the command's stdin, leaving its read direction open."""

    def close(self) -> None:
        """End the stream here, dropping whatever was not sent yet."""
        self._end(None)

    def abort(self, error: Exception) -> None:
        """End the stream here under ``error``, dropping whatever was not sent yet; the receiver takes ``error``."""
        self._end(error)

    def _end(self, error: Exception | None) -> None:
        """End the stream once, with ``error`` or ``None``: abort the raw transport and tell the receiver."""
        if self._ended:
            return
        self._ended = True
        if self._transport is not None:
            self._transport.abort()
        self._wake_drain()
        self._receiver.stream_ended(error)

    def _wake_drain(self) -> None:
        if self._drain_waiter is not None and not self._drain_waiter.done():
            self._drain_waiter.set_result(None)


class PlainCarrier(AttachCarrier):
    """Carries the stream as it is, over a unix socket or plain TCP.

    Stdin ends with the raw transport's ``write_eof()``, which sends the end of the write
    direction only once every byte buffered before it is sent.
    """

    def data_received(self, data: bytes) -> None:
        """Hand the engine's ``data`` to the receiver."""
        self._receiver.bytes_received(data)

    def write(self, data: bytes) -> None:
        """Send ``data`` to the engine."""
        self.transport.write(data)

    def end_input(self) -> None:
        """End the command's stdin with the transport's end of its write direction."""
        self.transport.write_eof()
