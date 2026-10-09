"""Harness self-test for :mod:`tai42_e2e.pgwire`: the tap decodes a Postgres client's
frontend messages — the startup phase, simple queries, unnamed and named extended-protocol
statements, a closed prepared statement — fed in arbitrary chunks, refuses an encrypted
stream loudly, and rides a :class:`~tai42_e2e.tcprelay.TcpRelay` as its client observer."""

from __future__ import annotations

import socket
import struct
import threading
from collections.abc import Iterator

import pytest

from tai42_e2e.pgwire import PgStatement, PgStatementTap
from tai42_e2e.tcprelay import TcpRelay, wait_relay_ready

# Pure harness self-test: it boots no stack and exercises no backend seam, so
# running it under every backend leg buys nothing.
pytestmark = [pytest.mark.backendless, pytest.mark.needs("no-stack")]


def _untyped(code: int, payload: bytes = b"") -> bytes:
    return struct.pack("!ii", 8 + len(payload), code) + payload


def _typed(kind: bytes, body: bytes) -> bytes:
    return kind + struct.pack("!i", 4 + len(body)) + body


def _parse(name: str, sql: str) -> bytes:
    return _typed(b"P", name.encode() + b"\x00" + sql.encode() + b"\x00" + struct.pack("!h", 0))


def _bind(name: str, params: list[bytes | None]) -> bytes:
    body = b"\x00" + name.encode() + b"\x00" + struct.pack("!h", 1) + struct.pack("!h", 0)
    body += struct.pack("!h", len(params))
    for param in params:
        body += struct.pack("!i", -1) if param is None else struct.pack("!i", len(param)) + param
    return _typed(b"B", body + struct.pack("!h", 0))


_SSL_REQUEST = _untyped(80877103)
_STARTUP = _untyped(196608, b"user\x00alice\x00\x00")


def _session() -> bytes:
    return b"".join(
        [
            _SSL_REQUEST,
            _STARTUP,
            _typed(b"Q", b"SET search_path TO public\x00"),
            _parse("", "SELECT version FROM t WHERE name = $1"),
            _bind("", [b"alpha"]),
            _typed(b"E", b"\x00" + struct.pack("!i", 0)),
            _typed(b"S", b""),
            _parse("_s1", "SELECT name FROM t WHERE name = ANY($1)"),
            _bind("_s1", [b"{alpha,beta}"]),
            _bind("_s1", [None]),
            _typed(b"C", b"S_s1\x00"),
        ]
    )


_EXPECTED = [
    PgStatement(sql="SET search_path TO public", params=()),
    PgStatement(sql="SELECT version FROM t WHERE name = $1", params=(b"alpha",)),
    PgStatement(sql="SELECT name FROM t WHERE name = ANY($1)", params=(b"{alpha,beta}",)),
    PgStatement(sql="SELECT name FROM t WHERE name = ANY($1)", params=(None,)),
]


@pytest.mark.parametrize("chunk", [1, 7, 4096])
def test_the_tap_records_every_statement_execution_whatever_the_chunking(chunk: int) -> None:
    tap = PgStatementTap()
    feed = tap.observer()
    data = _session()
    for start in range(0, len(data), chunk):
        feed(data[start : start + chunk])
    assert tap.since(0) == _EXPECTED
    assert [statement.carries("beta") for statement in tap.since(0)] == [False, False, True, False]


def test_mark_cuts_the_record() -> None:
    tap = PgStatementTap()
    feed = tap.observer()
    feed(_STARTUP + _typed(b"Q", b"SELECT 1\x00"))
    mark = tap.mark()
    feed(_typed(b"Q", b"SELECT 2\x00"))
    assert tap.since(mark) == [PgStatement(sql="SELECT 2", params=())]


def test_a_closed_prepared_statement_is_forgotten() -> None:
    feed = PgStatementTap().observer()
    feed(_STARTUP + _parse("_s1", "SELECT 1") + _typed(b"C", b"S_s1\x00"))
    with pytest.raises(ValueError, match="Bind names statement '_s1'"):
        feed(_bind("_s1", []))


def test_an_encrypted_stream_is_refused() -> None:
    feed = PgStatementTap().observer()
    feed(_SSL_REQUEST)
    # A TLS ClientHello record header where the startup message would be.
    with pytest.raises(ValueError, match="plaintext connections only"):
        feed(b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03")


class _SinkServer:
    """A loopback upstream that reads and discards everything sent to it."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            while conn.recv(4096):
                pass

    def __enter__(self) -> _SinkServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._sock.close()
        self._thread.join(timeout=5.0)


@pytest.fixture
def sink() -> Iterator[_SinkServer]:
    with _SinkServer() as server:
        yield server


def test_the_relay_feeds_the_client_stream_to_the_tap(sink: _SinkServer) -> None:
    tap = PgStatementTap()
    relay = TcpRelay("127.0.0.1", sink.port, observe_client=tap.observer)
    relay.start()
    try:
        wait_relay_ready(relay)
        with socket.create_connection((relay.listen_host, relay.port), timeout=2.0) as client:
            client.sendall(_session())
            client.shutdown(socket.SHUT_WR)
            client.settimeout(5.0)
            # The relay half-closes toward the client once the upstream has read everything.
            assert client.recv(1) == b""
        assert tap.since(0) == _EXPECTED
    finally:
        relay.stop()
    assert not relay.is_leaked()


def test_an_observer_that_cannot_read_the_stream_fails_the_relay(sink: _SinkServer) -> None:
    relay = TcpRelay("127.0.0.1", sink.port, observe_client=PgStatementTap().observer)
    relay.start()
    try:
        wait_relay_ready(relay)
        with socket.create_connection((relay.listen_host, relay.port), timeout=2.0) as client:
            client.sendall(_untyped(12345))
            client.settimeout(5.0)
            assert client.recv(1) == b""
    finally:
        with pytest.raises(RuntimeError, match="failed") as failure:
            relay.stop()
    assert "unknown startup message code 12345" in str(failure.value.__cause__)
    assert not relay.is_leaked()
