"""A Postgres wire-protocol tap: the statements a stack sends, read off the bytes a
:class:`~tai42_e2e.tcprelay.TcpRelay` forwards from the stack to Postgres.

The relay feeds each forwarded connection's client-to-server bytes to its own
:class:`PgConnectionReader`, which decodes the frontend messages and records every
statement execution on the shared :class:`PgStatementTap`: each simple ``Query`` and
each ``Bind`` of the extended protocol, with the SQL of the statement it binds (a
named prepared statement keeps the SQL its ``Parse`` gave) and its parameter values.
So a test can count the statements one request made — filtered by a value only that
request carries — without instrumenting the system under test.

The tap reads plaintext only: a connection that negotiates TLS or GSS encryption is a
loud error, never a silently empty record. Harness machinery, not the system under test.
"""

from __future__ import annotations

import struct
import threading
from collections.abc import Callable
from dataclasses import dataclass

# Untyped first-message codes (the protocol's startup phase).
_PROTOCOL_V3 = 196608
_SSL_REQUEST = 80877103
_GSSENC_REQUEST = 80877104
_CANCEL_REQUEST = 80877102


@dataclass(frozen=True)
class PgStatement:
    """One statement execution: its SQL and its parameter values (``None`` for SQL NULL)."""

    sql: str
    params: tuple[bytes | None, ...]

    def carries(self, value: str) -> bool:
        """Whether any parameter value contains ``value``'s UTF-8 bytes."""
        needle = value.encode()
        return any(param is not None and needle in param for param in self.params)


class PgStatementTap:
    """Every statement execution the tapped connections sent, in arrival order.

    :meth:`observer` is the relay's ``observe_client`` factory (one reader per forwarded
    connection); :meth:`mark` and :meth:`since` cut the record at a point in a test.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._statements: list[PgStatement] = []

    def observer(self) -> Callable[[bytes], None]:
        """The feed of a new reader for one connection's client-to-server byte stream."""
        return PgConnectionReader(self).feed

    def record(self, statement: PgStatement) -> None:
        with self._lock:
            self._statements.append(statement)

    def mark(self) -> int:
        """The current end of the record, for :meth:`since`."""
        with self._lock:
            return len(self._statements)

    def since(self, mark: int) -> list[PgStatement]:
        """Every statement recorded after ``mark``."""
        with self._lock:
            return list(self._statements[mark:])


class PgConnectionReader:
    """Decodes one connection's frontend messages and records its statement executions."""

    def __init__(self, tap: PgStatementTap) -> None:
        self._tap = tap
        self._buffer = bytearray()
        self._started = False
        self._prepared: dict[str, str] = {}

    def feed(self, data: bytes) -> None:
        """Consume the next chunk of client-to-server bytes, recording each complete statement."""
        self._buffer += data
        while self._next_message():
            pass

    def _next_message(self) -> bool:
        if not self._started:
            return self._next_startup_message()
        if len(self._buffer) < 5:
            return False
        kind = bytes(self._buffer[0:1])
        (length,) = struct.unpack_from("!i", self._buffer, 1)
        if length < 4:
            raise ValueError(f"malformed frontend message {kind!r}: length {length}")
        if len(self._buffer) < 1 + length:
            return False
        body = bytes(self._buffer[5 : 1 + length])
        del self._buffer[: 1 + length]
        self._message(kind, body)
        return True

    def _next_startup_message(self) -> bool:
        if len(self._buffer) < 8:
            return False
        length, code = struct.unpack_from("!ii", self._buffer, 0)
        if length < 8 or length > 10_000:
            raise ValueError(
                f"unreadable startup message (length {length}): the statement tap reads plaintext "
                "connections only, and this one is encrypted or not Postgres"
            )
        if len(self._buffer) < length:
            return False
        del self._buffer[:length]
        if code == _PROTOCOL_V3:
            self._started = True
        elif code not in (_SSL_REQUEST, _GSSENC_REQUEST, _CANCEL_REQUEST):
            raise ValueError(f"unknown startup message code {code}")
        return True

    def _message(self, kind: bytes, body: bytes) -> None:
        if kind == b"Q":
            sql, _ = _cstring(body, 0)
            self._tap.record(PgStatement(sql=sql, params=()))
        elif kind == b"P":
            name, offset = _cstring(body, 0)
            sql, _ = _cstring(body, offset)
            self._prepared[name] = sql
        elif kind == b"B":
            _portal, offset = _cstring(body, 0)
            name, offset = _cstring(body, offset)
            if name not in self._prepared:
                raise ValueError(f"Bind names statement {name!r} that no Parse on this connection prepared")
            (n_formats,) = struct.unpack_from("!h", body, offset)
            offset += 2 + 2 * n_formats
            (n_params,) = struct.unpack_from("!h", body, offset)
            offset += 2
            params: list[bytes | None] = []
            for _ in range(n_params):
                (size,) = struct.unpack_from("!i", body, offset)
                offset += 4
                if size < 0:
                    params.append(None)
                else:
                    params.append(body[offset : offset + size])
                    offset += size
            self._tap.record(PgStatement(sql=self._prepared[name], params=tuple(params)))
        elif kind == b"C" and body[:1] == b"S":
            name, _ = _cstring(body, 1)
            self._prepared.pop(name, None)


def _cstring(body: bytes, offset: int) -> tuple[str, int]:
    """The NUL-terminated string at ``offset`` and the offset just past its terminator."""
    end = body.index(b"\x00", offset)
    return body[offset:end].decode(), end + 1
