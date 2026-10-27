"""A loopback TCP forwarder to one upstream, run as its own process and driven over stdin.

Usage: ``python _tcp_relay.py UPSTREAM_HOST UPSTREAM_PORT [--drop-first]``.

Each stdin line is a command, answered with one stdout line once it is done:

- ``open``: listen on ``127.0.0.1`` (a free port the first time, the same port after)
  and forward every accepted connection to the upstream; answers ``listening <port>``.
- ``close``: stop listening and close every forwarded connection, as a Redis restart
  does; a connection accepted while the close runs is closed too, never forwarded.
  Answers ``closed <dropped_first>``, the number of connections ``--drop-first`` closed
  unanswered (1 once the first connection came, 0 before it or without the flag).

With ``--drop-first`` the first accepted connection is closed unanswered, as a proxy
with no upstream yet does. A failure to accept or to reach the upstream closes the
listener and the connection it was serving, and the process exits with that error at
its next command or when its stdin closes. The process exits when its stdin closes.
"""

from __future__ import annotations

import contextlib
import socket
import sys
import threading
from collections.abc import Iterable


class _Forwarder:
    def __init__(self, upstream: tuple[str, int], *, drop_first: bool) -> None:
        self._upstream = upstream
        self._drop_first = drop_first
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._listener: socket.socket | None = None
        self._failure: BaseException | None = None
        self.port = 0
        self.dropped_first = 0

    def open(self) -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen()
        self.port = listener.getsockname()[1]
        with self._lock:
            self._listener = listener
        threading.Thread(target=self._accept, args=(listener,), daemon=True).start()

    def close(self) -> None:
        with self._lock:
            sockets, self._sockets = self._sockets, []
            listener, self._listener = self._listener, None
        _close_all([listener, *sockets])

    def raise_failure(self) -> None:
        """Exit the process with the error that ended the forwarding, if one did."""
        if self._failure is not None:
            raise SystemExit(f"the relay stopped forwarding: {self._failure!r}")

    def _accept(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError as error:
                self._stop_on(listener, error, [])
                return
            if self._drop_first and not self.dropped_first:
                self.dropped_first += 1
                client.close()
                continue
            try:
                upstream = socket.create_connection(self._upstream)
            except OSError as error:
                self._stop_on(listener, error, [client])
                return
            with self._lock:
                # A close that ran since the accept took this listener away: the pair is
                # closed here, never forwarded.
                forwarding = self._listener is listener
                if forwarding:
                    self._sockets += [client, upstream]
            if not forwarding:
                _close_all([client, upstream])
                return
            threading.Thread(target=self._pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=self._pipe, args=(upstream, client), daemon=True).start()

    def _stop_on(self, listener: socket.socket, error: OSError, sockets: list[socket.socket]) -> None:
        """End the accept loop of ``listener``: an error on a listener still open is the relay's failure."""
        with self._lock:
            failed = self._listener is listener
            if failed:
                self._listener = None
                self._failure = error
        _close_all([listener, *sockets] if failed else sockets)

    @staticmethod
    def _pipe(source: socket.socket, target: socket.socket) -> None:
        # Closing the relay breaks its pipes; a broken pipe ends the forwarding, as a dropped link does.
        with contextlib.suppress(OSError):
            while data := source.recv(65536):
                target.sendall(data)
        for sock in (source, target):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


def _close_all(sockets: Iterable[socket.socket | None]) -> None:
    for sock in sockets:
        if sock is not None:
            # A socket the far side already shut down refuses a second shutdown.
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()


def main(upstream: tuple[str, int], *, drop_first: bool) -> None:
    forwarder = _Forwarder(upstream, drop_first=drop_first)
    for line in sys.stdin:
        forwarder.raise_failure()
        command = line.strip()
        if command == "open":
            forwarder.open()
            print(f"listening {forwarder.port}", flush=True)
        elif command == "close":
            forwarder.close()
            print(f"closed {forwarder.dropped_first}", flush=True)
        else:
            raise SystemExit(f"unknown relay command: {command!r}")
    forwarder.close()
    forwarder.raise_failure()


if __name__ == "__main__":
    host, port, *flags = sys.argv[1:]
    main((host, int(port)), drop_first="--drop-first" in flags)
