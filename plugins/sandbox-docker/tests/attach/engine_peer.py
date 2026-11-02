"""A fake Docker engine on a real socket: one hijacked exec stream, served from a thread.

The peer is a blocking socket, a :class:`ssl.SSLSocket` over TLS, so it models what a
daemon does after the client ends its stdin: OpenSSL's blocking socket reads the client's
``close_notify`` as the end of the input and still writes the output after it.
"""

from __future__ import annotations

import contextlib
import os
import socket
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import trustme

# How long a peer waits for its client before the test fails instead of hanging.
_PEER_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class EngineCerts:
    """A CA, the engine's certificate it signed, and the client identity it signed."""

    ca: trustme.CA
    server_context: ssl.SSLContext
    client_context: ssl.SSLContext


def engine_certs() -> EngineCerts:
    """Certificates for an mTLS engine on the loopback address, and the client identity that speaks to it."""
    ca = trustme.CA()
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1", "localhost").configure_cert(server_context)
    ca.configure_trust(server_context)
    server_context.verify_mode = ssl.CERT_REQUIRED
    client_context = ssl.create_default_context()
    ca.configure_trust(client_context)
    ca.issue_cert("sandbox-client").configure_cert(client_context)
    return EngineCerts(ca=ca, server_context=server_context, client_context=client_context)


class PeerConnection:
    """The engine's side of one attach connection."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock

    def answer(self, status: int = 101, body: bytes = b"", *, length: bool = True) -> None:
        """Answer the hijack request: ``101`` upgrades the stream, any other status refuses with ``body``.

        A refusal names its body's length, or (``length`` false) runs the body to the connection's end.
        """
        if status == 101:
            self.sock.sendall(
                b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n"
                b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n"
            )
            return
        head = f"HTTP/1.1 {status} Refused\r\nContent-Type: application/json\r\n"
        if length:
            head += f"Content-Length: {len(body)}\r\n"
        self.sock.sendall(head.encode() + b"\r\n" + body)

    def read_stdin(self) -> int:
        """Read the command's stdin to its end; returns how many bytes came."""
        total = 0
        while chunk := self.sock.recv(1 << 16):
            total += len(chunk)
        return total

    def send_frame(self, stream: int, data: bytes) -> None:
        """Send one output frame on ``stream`` (1 stdout, 2 stderr)."""
        self.sock.sendall(bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data)

    def send_raw(self, data: bytes) -> None:
        """Send ``data`` on the raw connection, under any TLS layer."""
        with socket.socket(fileno=os.dup(self.sock.fileno())) as raw:
            raw.sendall(data)

    def wait_for_client_close(self) -> None:
        """Wait, reading nothing, until the client ends the connection."""
        with contextlib.suppress(OSError):
            while self.sock.recv(1 << 16):
                pass
        self.sock.close()

    def end_output(self) -> None:
        """End the output as the engine does once the command and its stdin are done."""
        if isinstance(self.sock, ssl.SSLSocket):
            # The engine's close_notify. The client answers with its own when it has not yet
            # ended its stdin, or drops the connection at the alert, ending the stream.
            with contextlib.suppress(OSError):
                self.sock = self.sock.unwrap()
        self.sock.close()

    def end_output_while_stdin_flows(self) -> None:
        """End the output while the client may still write stdin, then drop what it writes."""
        self.sock.shutdown(socket.SHUT_WR)
        self.wait_for_client_close()


@dataclass
class EnginePeer:
    """One fake engine connection: it reads the hijack request, then runs ``script`` on the connection."""

    script: Callable[[PeerConnection], None]
    tls: ssl.SSLContext | None = None
    unix_path: str | None = None
    request: bytes = b""
    error: BaseException | None = None
    # The loopback port a TCP peer listens on.
    port: int = field(init=False, default=0)
    _listener: socket.socket = field(init=False)
    _thread: threading.Thread = field(init=False)

    def __post_init__(self) -> None:
        if self.unix_path is not None:
            self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener.bind(self.unix_path)
        else:
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.bind(("127.0.0.1", 0))
            self.port = self._listener.getsockname()[1]
        self._listener.listen(1)
        self._listener.settimeout(_PEER_TIMEOUT_SECONDS)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def join(self) -> None:
        """Wait for the peer to finish its script; a failure inside it fails the test."""
        self._thread.join(_PEER_TIMEOUT_SECONDS)
        assert not self._thread.is_alive(), "the fake engine did not finish"
        self._listener.close()
        if self.error is not None:
            raise self.error

    def _serve(self) -> None:
        try:
            raw, _ = self._listener.accept()
            raw.settimeout(_PEER_TIMEOUT_SECONDS)
            sock = self.tls.wrap_socket(raw, server_side=True) if self.tls is not None else raw
            self.request = _read_request(sock)
            self.script(PeerConnection(sock))
        except BaseException as exc:
            self.error = exc  # join() raises it in the test


def _read_request(sock: socket.socket) -> bytes:
    """Read the client's hijack request, its head and its body."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        assert chunk, "the client closed before its request was whole"
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = next(
        int(line.split(b":")[1]) for line in head.split(b"\r\n") if line.lower().startswith(b"content-length")
    )
    while len(body) < length:
        body += sock.recv(4096)
    return head + b"\r\n\r\n" + body


def refuse(status: int, body: bytes, *, length: bool = True) -> Callable[[PeerConnection], None]:
    """An engine that refuses the attach with ``status`` and ``body`` (see :meth:`PeerConnection.answer`)."""

    def script(conn: PeerConnection) -> None:
        conn.answer(status, body, length=length)
        conn.end_output()

    return script


def answer_and_wait(conn: PeerConnection) -> None:
    """An engine whose command never writes: it upgrades the stream, then waits for the client to end it."""
    conn.answer()
    conn.wait_for_client_close()


def count_stdin(conn: PeerConnection) -> None:
    """A ``cat | wc -c`` behind the engine, slow to start reading so the client's buffer fills first."""
    conn.answer()
    time.sleep(0.2)
    count = conn.read_stdin()
    conn.send_frame(1, str(count).encode())
    conn.end_output()


def take_one_byte(conn: PeerConnection) -> None:
    """A ``head -c 1`` behind the engine: one byte in, one byte out, and the output ends."""
    conn.answer()
    data = conn.sock.recv(1)
    conn.send_frame(1, data)
    conn.end_output_while_stdin_flows()
