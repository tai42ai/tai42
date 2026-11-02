"""The TLS carrier of an exec attachment: the engine's mTLS over :class:`ssl.SSLObject`.

The command's stdin ends with a TLS ``close_notify`` alert, the half-close TLS defines
(RFC 8446 §6.1) and the one the Docker CLI sends over TLS: the alert ends the write
direction only, so the engine closes the command's stdin and its output still comes back.
The alert and the stdin bytes before it go out in order through the one raw transport, so
the end of the input never overtakes its data. Certificate and hostname checks are the
:mod:`ssl` module's own, through the context the carrier is given.
"""

from __future__ import annotations

import asyncio
import ssl

from tai42_contract.sandbox import SandboxError

from tai42_sandbox_docker.attach_carrier import AttachCarrier, StreamReceiver

# The most plaintext one read from the TLS layer takes.
_READ_SIZE = 1 << 16


class TlsCarrier(AttachCarrier):
    """Carries the stream over TLS: a handshake, records each way, and stdin ended by ``close_notify``.

    The engine's own ``close_notify``, or its end of the raw connection, ends the stream; any
    other TLS failure, in the handshake or after it (as the TLS 1.3 alert of an engine that
    refuses the client's certificate once the client's handshake is done), ends it with its
    :class:`ssl.SSLError`.
    """

    def __init__(self, receiver: StreamReceiver, *, context: ssl.SSLContext, server_hostname: str) -> None:
        """Run TLS under ``context`` to the engine at ``server_hostname``, handing its plaintext to ``receiver``."""
        super().__init__(receiver)
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._tls = context.wrap_bio(self._incoming, self._outgoing, server_hostname=server_hostname)
        self._handshake: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Start the TLS handshake on the raw ``transport``."""
        super().connection_made(transport)
        self._pump_handshake()

    def data_received(self, data: bytes) -> None:
        """Take the engine's TLS records: the handshake's, then the stream's."""
        self._incoming.write(data)
        if self._handshake.done():
            self._pump_read()
        else:
            self._pump_handshake()

    def connection_lost(self, exc: Exception | None) -> None:
        """End the stream; a handshake still under way fails with the lost connection."""
        if not self._handshake.done():
            self._handshake.set_exception(
                exc or ConnectionResetError("the Docker engine closed the connection during the TLS handshake")
            )
        super().connection_lost(exc)

    def close(self) -> None:
        """End the stream here; a handshake still under way ends with it, as nothing waits for it any more."""
        if not self._handshake.done():
            self._handshake.cancel()
        elif not self._handshake.cancelled():
            self._handshake.exception()
        super().close()

    async def start(self) -> None:
        """Wait for the TLS handshake; a failed one raises its :class:`ssl.SSLError` or connection error."""
        await self._handshake

    def write(self, data: bytes) -> None:
        """Encrypt ``data`` and send it to the engine."""
        view = memoryview(data)
        while view:
            written = self._tls.write(view)
            view = view[written:]
        self._flush()

    def end_input(self) -> None:
        """End the command's stdin with a ``close_notify`` alert, leaving the read direction open.

        The first ``unwrap()`` queues the alert and then wants the engine's own alert, which
        the read side takes later as the stream's end; that want is the expected result.
        """
        try:
            self._tls.unwrap()
        except ssl.SSLWantReadError:
            pass
        except ssl.SSLError as exc:
            raise SandboxError(f"cannot close stdin: the sandbox exec stream's TLS refused its close: {exc}") from exc
        self._flush()

    def _flush(self) -> None:
        """Send every TLS record the layer has queued."""
        pending = self._outgoing.read()
        if pending and not self.ended:
            self.transport.write(pending)

    def _pump_handshake(self) -> None:
        try:
            self._tls.do_handshake()
        except ssl.SSLWantReadError:
            self._flush()
            return
        except ssl.SSLError as exc:
            self._flush()  # the alert that tells the engine why
            self._handshake.set_exception(exc)
            self._end(exc)
            return
        self._flush()
        self._handshake.set_result(None)
        self._pump_read()

    def _pump_read(self) -> None:
        while not self.ended:
            try:
                data = self._tls.read(_READ_SIZE)
            except ssl.SSLWantReadError:
                break
            except ssl.SSLZeroReturnError:
                self._end(None)  # the engine's close_notify
                return
            except ssl.SSLError as exc:
                self._end(exc)
                return
            if not data:
                self._end(None)
                return
            self._receiver.bytes_received(data)
        # Records the layer queued while reading, as a key update's answer, go out now.
        self._flush()
