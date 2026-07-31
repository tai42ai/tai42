"""Harness self-test for :mod:`tai42_e2e.tcprelay`: the relay round-trips bytes to
its upstream, ``sever`` drops live connections and refuses new ones, ``restore``
re-opens the same port, and a stopped relay reports no leaked listener/thread (the
teardown assertion the stack relies on)."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

from tai42_e2e.tcprelay import TcpRelay, wait_relay_ready

# Pure harness self-test: it boots no stack and exercises no backend seam, so
# running it under every backend leg buys nothing.
pytestmark = pytest.mark.backendless


class _EchoServer:
    """A threaded loopback TCP echo upstream — the relay's target under test."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn: socket.socket) -> None:
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                conn.sendall(data)

    def __enter__(self) -> _EchoServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=5.0)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    sock.settimeout(2.0)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


@pytest.fixture
def echo() -> Iterator[_EchoServer]:
    with _EchoServer() as server:
        yield server


def test_relay_round_trips_bytes_to_upstream(echo: _EchoServer) -> None:
    with TcpRelay("127.0.0.1", echo.port) as relay:
        wait_relay_ready(relay)
        with socket.create_connection(("127.0.0.1", relay.port), timeout=2.0) as client:
            client.sendall(b"hello-relay")
            assert _recv_exact(client, len(b"hello-relay")) == b"hello-relay"
    # The context manager stopped it; nothing leaked.
    assert relay.is_leaked() is False


def test_sever_drops_live_connection_and_refuses_new(echo: _EchoServer) -> None:
    relay = TcpRelay("127.0.0.1", echo.port)
    relay.start()
    wait_relay_ready(relay)
    port = relay.port

    live = socket.create_connection(("127.0.0.1", relay.port), timeout=2.0)
    live.sendall(b"a")
    assert _recv_exact(live, 1) == b"a"

    relay.sever()
    # The live connection is dropped mid-stream: recv unblocks with EOF.
    assert _recv_exact(live, 1) == b""
    live.close()
    # New connections are refused while the outage is in effect (listener closed).
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=1.0).close()

    # Restore re-opens the SAME port; a fresh connection round-trips again.
    relay.restore()
    wait_relay_ready(relay)
    assert relay.port == port
    with socket.create_connection(("127.0.0.1", relay.port), timeout=2.0) as client:
        client.sendall(b"b")
        assert _recv_exact(client, 1) == b"b"

    relay.stop()
    assert relay.is_leaked() is False


def test_stopped_relay_reports_no_leak(echo: _EchoServer) -> None:
    relay = TcpRelay("127.0.0.1", echo.port)
    relay.start()
    wait_relay_ready(relay)
    # A started, unstopped relay holds a live listener — the very state teardown
    # asserts against once it has stopped every attached relay.
    assert relay.is_leaked() is True
    relay.stop()
    assert relay.is_leaked() is False
