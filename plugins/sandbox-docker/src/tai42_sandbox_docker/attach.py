"""The exec attachment: an exec's hijacked engine stream, from its start to its end.

:meth:`ExecAttachment.open` dials the engine endpoint on a carrier of its own
(:mod:`~tai42_sandbox_docker.attach_carrier`, and :mod:`~tai42_sandbox_docker.attach_tls`
over mTLS), sends the exec's start as the hijack request the Docker Engine API defines
(``POST /exec/{id}/start`` upgraded to a raw stream), and checks the ``101`` answer. From
there the attachment demultiplexes the engine's 8-byte-header frames into stdout and stderr
chunks, writes the command's stdin with backpressure, and ends it through
:meth:`ExecAttachment.end_input`, the one end of stdin every door shares.

The engine's end of the stream, a lost connection, or a close here aborts the connection,
dropping stdin the command never took, and ends the output: a write then reports the
stream closed and :meth:`~ExecAttachment.end_input` raises. A TLS failure after the engine's
answer, or the engine ending the stream inside an output frame, is an error the output ends
with; a close here inside a frame drops the part of it that came, as the close drops all
output the engine sends after it.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
from collections import deque
from collections.abc import Awaitable, Callable

from aiodocker.exceptions import DockerError  # pyright: ignore[reportMissingImports]
from tai42_contract.sandbox import SandboxError, SandboxStreamChunk

from tai42_sandbox_docker.attach_carrier import AttachCarrier, PlainCarrier
from tai42_sandbox_docker.attach_tls import TlsCarrier
from tai42_sandbox_docker.engine_endpoint import EngineEndpoint, TcpEndpoint, UnixSocketEndpoint

# The Docker exec multiplex frame header (see the Docker Engine attach "Stream Format"):
# byte 0 is the stream id (1 stdout, 2 stderr), bytes 4-7 the payload size, big-endian.
_FRAME_HEADER_SIZE = 8
_STREAM_STDERR = 2

# The end of the engine's answer head to the hijack request.
_HEAD_END = b"\r\n\r\n"

# Unread output the attachment holds before it stops reading from the engine, and the level
# it reads again at.
_READ_HIGH_WATER = 1 << 17
_READ_LOW_WATER = 1 << 16

# The most stdin one write hands the carrier before it waits for the transport to drain.
_WRITE_CHUNK = 1 << 16


# Opens the attached stream of a started exec, by the exec's engine id.
ExecAttacher = Callable[[str], Awaitable["ExecAttachment"]]


def _hijack_request(exec_id: str, authority: str) -> bytes:
    """The engine request that starts exec ``exec_id`` attached and upgrades the connection to its stream."""
    body = json.dumps({"Detach": False, "Tty": False}).encode()
    head = (
        f"POST /exec/{exec_id}/start HTTP/1.1\r\n"
        f"Host: {authority}\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: tcp\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    )
    return head.encode() + body


class _AnswerHead:
    """The status and headers of the engine's answer to the hijack request."""

    def __init__(self, head: bytes) -> None:
        """Read ``head``; one whose status line names no status raises :class:`SandboxError`."""
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        try:
            self.status = int(status_line.split(" ", 2)[1])
        except (IndexError, ValueError) as exc:
            raise SandboxError(f"the Docker engine answered the exec attach with {status_line!r}") from exc
        self.headers = {}
        for line in header_lines:
            name, _, value = line.partition(":")
            self.headers[name.strip().lower()] = value.strip()

    @property
    def content_length(self) -> int | None:
        value = self.headers.get("content-length")
        return int(value) if value is not None and value.isdigit() else None


def _refusal(status: int, body: bytes) -> DockerError:
    """The engine error of a refused attach: its status and the message its JSON body carries."""
    text = body.decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text)
    except ValueError:
        return DockerError(status, text)
    message = payload.get("message") if isinstance(payload, dict) else None
    return DockerError(status, message if isinstance(message, str) else text)


class ExecAttachment:
    """One exec's attached engine stream; see the module docstring."""

    def __init__(self) -> None:
        """An attachment with no connection yet; :meth:`open` builds a connected one."""
        self._carrier: AttachCarrier | None = None
        self._buffer = bytearray()
        self._head: _AnswerHead | None = None
        self._upgraded: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._chunks: deque[SandboxStreamChunk] = deque()
        self._unread = 0
        self._reading_paused = False
        self._reader: asyncio.Future[None] | None = None
        self._ended = False
        self._error: Exception | None = None
        self._input_ended = False
        # Set by a close here: the output ends where the close cut it, never with an error.
        self._closed_here = False

    @classmethod
    async def open(
        cls, endpoint: EngineEndpoint, exec_id: str, *, ssl_context: ssl.SSLContext | None
    ) -> ExecAttachment:
        """Start exec ``exec_id`` attached to a stream from the engine at ``endpoint``.

        A TLS endpoint runs under ``ssl_context`` (the mTLS client identity), or the default
        context when none is given. The engine refusing the attach raises its
        :class:`DockerError`; a connection or TLS failure before the engine's answer its
        :class:`OSError`, an :class:`ssl.SSLError` for a TLS one, the alert of an engine that
        refuses the client's certificate after the handshake included. An attach that fails or
        is cancelled aborts its connection.
        """
        attachment = cls()
        try:
            carrier = await attachment._connect(endpoint, ssl_context)
            await carrier.start()
            carrier.write(_hijack_request(exec_id, _authority(endpoint)))
            await attachment._upgraded
        except BaseException:
            # The carrier exists once the loop made it, even when the connect itself failed
            # or was cancelled after that.
            if attachment._carrier is not None:
                attachment._carrier.close()
            attachment._drop_answer()
            raise
        return attachment

    async def read(self) -> SandboxStreamChunk | None:
        """The next chunk of output, or ``None`` at the stream's end.

        A stream that ended on a failure of its own (a TLS failure, its :class:`ssl.SSLError`
        the cause, or an output frame cut short) raises :class:`SandboxError` once the output
        before it is read. A lost connection ends
        the stream as its end does: the exit code the engine records tells how the command
        ended. A cancelled read loses no output.
        """
        while not self._chunks:
            if self._ended:
                if self._error is not None:
                    raise self._error
                return None
            self._reader = asyncio.get_running_loop().create_future()
            try:
                await self._reader
            finally:
                self._reader = None
        chunk = self._chunks.popleft()
        self._unread -= len(chunk.data)
        if self._reading_paused and self._unread <= _READ_LOW_WATER:
            self._reading_paused = False
            self._connected().resume_reading()
        return chunk

    def take_received(self) -> list[SandboxStreamChunk]:
        """Take every chunk already received and not yet read, without waiting.

        The stream's end and its error stay for :meth:`read`, which returns them next once the
        stream has ended.
        """
        chunks = list(self._chunks)
        self._chunks.clear()
        self._unread = 0
        if self._reading_paused:
            self._reading_paused = False
            self._connected().resume_reading()
        return chunks

    async def write(self, data: bytes) -> bool:
        """Write ``data`` to the command's stdin; returns whether the stream took all of it.

        ``False`` means the stream ended, or its stdin did, and the data not yet sent was
        dropped.
        """
        carrier = self._connected()
        for start in range(0, len(data), _WRITE_CHUNK):
            if self._input_ended or carrier.ended:
                return False
            carrier.write(data[start : start + _WRITE_CHUNK])
            await carrier.drain()
        return not (self._input_ended or carrier.ended)

    def end_input(self) -> None:
        """End the command's stdin, leaving the output open; every byte written before it reaches the command.

        A stream that has ended raises :class:`SandboxError`: the command never sees the end
        of its input.
        """
        carrier = self._connected()
        if carrier.ended:
            raise SandboxError("cannot close stdin: the sandbox exec stream is closed")
        if self._input_ended:
            return
        self._input_ended = True
        carrier.end_input()

    def close(self) -> None:
        """End the stream here, dropping stdin not yet sent; a waiting read ends.

        The output ends after the whole frames already received; the part of a frame still
        arriving is dropped with the rest of the output the close cuts off.
        """
        self._closed_here = True
        self._connected().close()

    # -- the carrier's receiver -----------------------------------------------------------

    def bytes_received(self, data: bytes) -> None:
        """Take bytes from the engine: the answer to the hijack request, then output frames."""
        if self._ended:
            return
        self._buffer.extend(data)
        if not self._upgraded.done() and not self._take_answer():
            return
        self._take_frames()
        if not self._reading_paused and self._unread > _READ_HIGH_WATER:
            self._reading_paused = True
            self._connected().pause_reading()

    def stream_ended(self, error: Exception | None) -> None:
        """End the stream: the engine's answer, if it never came, and the output."""
        self._ended = True
        if not self._upgraded.done():
            self._upgraded.set_exception(self._unanswered(error))
        elif isinstance(error, ssl.SSLError):
            self._error = SandboxError(f"the sandbox exec stream's TLS failed: {error}")
            self._error.__cause__ = error
        elif not isinstance(error, OSError) and error is not None:
            self._error = error
        elif self._buffer and not self._closed_here:
            self._error = SandboxError(
                f"the sandbox exec stream ended inside an output frame ({len(self._buffer)} bytes left over)"
            )
        self._wake_reader()

    # -- the parts --------------------------------------------------------------------------

    async def _connect(self, endpoint: EngineEndpoint, ssl_context: ssl.SSLContext | None) -> AttachCarrier:
        """Dial the engine at ``endpoint`` on a carrier the attachment keeps from the moment the loop makes it."""
        loop = asyncio.get_running_loop()
        if isinstance(endpoint, UnixSocketEndpoint):
            _, carrier = await loop.create_unix_connection(lambda: self._keep(PlainCarrier(self)), endpoint.path)
        else:
            host, port = endpoint.address()
            if endpoint.tls:
                context = ssl_context if ssl_context is not None else ssl.create_default_context()
                factory = lambda: self._keep(TlsCarrier(self, context=context, server_hostname=host))  # noqa: E731
            else:
                factory = lambda: self._keep(PlainCarrier(self))  # noqa: E731
            transport, carrier = await loop.create_connection(factory, host, port)
            sock = transport.get_extra_info("socket")
            if sock is not None:
                # A long-lived stream: the keepalive notices an engine that went away unseen.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        return carrier

    def _keep(self, carrier: AttachCarrier) -> AttachCarrier:
        self._carrier = carrier
        return carrier

    def _connected(self) -> AttachCarrier:
        if self._carrier is None:
            raise RuntimeError("the exec attachment is not connected")
        return self._carrier

    def _take_answer(self) -> bool:
        """Read the engine's answer once it is whole; returns whether the stream is upgraded.

        An answer that is not HTTP aborts the connection with its :class:`SandboxError`, which
        the attach raises.
        """
        if self._head is None:
            end = self._buffer.find(_HEAD_END)
            if end < 0:
                return False
            try:
                self._head = _AnswerHead(bytes(self._buffer[:end]))
            except SandboxError as exc:
                self._connected().abort(exc)
                return False
            del self._buffer[: end + len(_HEAD_END)]
        if self._head.status == 101:
            self._upgraded.set_result(None)
            return True
        length = self._head.content_length
        if length is not None and len(self._buffer) >= length:
            self._upgraded.set_exception(_refusal(self._head.status, bytes(self._buffer[:length])))
            self._buffer.clear()
            self._connected().close()
        return False

    def _drop_answer(self) -> None:
        """Let go of the engine's answer of an attach that stopped before it: nothing waits for it any more."""
        if not self._upgraded.done():
            self._upgraded.cancel()
        elif not self._upgraded.cancelled():
            self._upgraded.exception()

    def _unanswered(self, error: Exception | None) -> BaseException:
        """The error of a stream that ended before the engine's answer to the hijack request was read."""
        if self._head is not None:
            # A refusal whose body runs to the connection's end.
            return _refusal(self._head.status, bytes(self._buffer))
        if error is not None:
            return error
        return ConnectionResetError("the Docker engine closed the exec attach connection before it answered")

    def _take_frames(self) -> None:
        buffer = self._buffer
        while len(buffer) >= _FRAME_HEADER_SIZE:
            size = int.from_bytes(buffer[4:_FRAME_HEADER_SIZE], "big")
            end = _FRAME_HEADER_SIZE + size
            if len(buffer) < end:
                break
            stream = "stderr" if buffer[0] == _STREAM_STDERR else "stdout"
            self._chunks.append(SandboxStreamChunk(stream=stream, data=bytes(buffer[_FRAME_HEADER_SIZE:end])))
            self._unread += size
            del buffer[:end]
        self._wake_reader()

    def _wake_reader(self) -> None:
        if self._reader is not None and not self._reader.done():
            self._reader.set_result(None)


def _authority(endpoint: EngineEndpoint) -> str:
    """The ``Host`` a request to the engine at ``endpoint`` names: a socket has none, so ``localhost``."""
    return endpoint.authority if isinstance(endpoint, TcpEndpoint) else "localhost"
