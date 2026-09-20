"""An in-process TCP forwarder the harness puts in front of Redis / Postgres so a
test can sever and restore a stack's connection to shared infra mid-run.

The compose infra is shared across concurrently-alive stacks, so an outage cannot
be injected by stopping the container (it would take down every other stack).
Instead a per-stack relay forwards ``127.0.0.1:<relay port>`` to the real backing
server; :meth:`sever` closes the listener AND drops every live connection (the
outage), and :meth:`restore` re-opens the listener on the same port (recovery,
without restarting the stack — the SUT's connection pools simply re-establish).

The listen port comes from :mod:`tai42_e2e.ports`, so it is reserved for the run and
cannot be handed to another stack — including across the sever/restore window, when
nothing is bound to it. Same in-process-thread idiom as :mod:`tai42_e2e._threaded`:
this is harness machinery, not the system under test. It is leak-checked in stack
teardown like a port or a broker vhost — a relay left listening, a forwarded
connection left open, or a thread left alive is a teardown ``RuntimeError``."""

from __future__ import annotations

import contextlib
import socket
import threading

from tai42_e2e import ports
from tai42_e2e.waiting import wait_for


class TcpRelay:
    """A threaded TCP forwarder to one upstream ``(host, port)``.

    ``start`` binds a reserved loopback port and accepts connections, each pumped
    bidirectionally to the upstream on two daemon threads. ``sever`` models an
    outage (listener closed, live connections dropped); ``restore`` re-binds the
    same port; ``stop`` is the final teardown and releases the port. Not reusable
    after ``stop``.

    A relay thread that dies of anything other than the deliberate close records
    the exception; :meth:`raise_if_failed` (called by :func:`wait_relay_ready` and
    by ``stop``) re-raises it on the owning thread, so a broken relay surfaces as
    itself instead of as an outage no test injected."""

    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self._upstream = (upstream_host, upstream_port)
        # Loopback only: the port comes from ``tai42_e2e.ports``, which proves a port
        # free on 127.0.0.1, and the relay fronts host-local infra.
        self._listen_host = "127.0.0.1"
        self._port: int | None = None
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._retired_accept_threads: list[threading.Thread] = []
        self._pump_threads: list[threading.Thread] = []
        self._live_conns: list[socket.socket] = []
        self._lock = threading.Lock()
        self._stopped = False
        self._failure: BaseException | None = None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Reserve a loopback port, bind the listener on it, and begin accepting."""
        if self._port is not None:
            raise RuntimeError("relay already started")
        self._port = ports.allocate_port()
        self._open_listener()

    def sever(self) -> None:
        """Inject the outage: close the listener so new connections are refused,
        and drop every live forwarded connection so in-flight requests error out.
        The port stays reserved so :meth:`restore` can re-bind it."""
        with self._lock:
            if self._listener is not None:
                self._listener.close()
                self._listener = None
            conns = list(self._live_conns)
            self._live_conns.clear()
        # Join the accept loop outside the lock (it holds no lock while blocking).
        # A thread that will not join is KEPT so ``is_leaked`` can still see it.
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=5.0)
            if self._accept_thread.is_alive():
                # It will not join: keep it visible to the leak check instead of
                # letting a later restore() overwrite (and hide) it.
                self._retired_accept_threads.append(self._accept_thread)
            self._accept_thread = None
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            conn.close()

    def restore(self) -> None:
        """End the outage: re-open the listener on the same port so fresh
        connections succeed again (the stack recovers without a restart)."""
        if self._port is None:
            raise RuntimeError("cannot restore a relay that was never started")
        if self._stopped:
            raise RuntimeError("cannot restore a relay that was stopped")
        if self._listener is not None:
            return
        self._open_listener()

    def stop(self) -> None:
        """Final teardown: close the listener, drop live connections, join every
        thread, release the reserved port, and re-raise a relay-thread failure.
        Idempotent."""
        already_stopped = self._stopped
        self._stopped = True
        self.sever()
        for thread in [*self._pump_threads, *self._retired_accept_threads]:
            thread.join(timeout=5.0)
        self._pump_threads = [t for t in self._pump_threads if t.is_alive()]
        self._retired_accept_threads = [t for t in self._retired_accept_threads if t.is_alive()]
        if self._port is not None and not already_stopped:
            ports.release_port(self._port)
        self.raise_if_failed()

    def __enter__(self) -> TcpRelay:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---- introspection ---------------------------------------------------

    @property
    def listen_host(self) -> str:
        return self._listen_host

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("relay not started")
        return self._port

    def raise_if_failed(self) -> None:
        """Re-raise the first unexpected exception a relay thread died of."""
        failure = self._failure
        if failure is not None:
            self._failure = None
            raise RuntimeError(f"relay to {self._upstream[0]}:{self._upstream[1]} failed") from failure

    def is_leaked(self) -> bool:
        """Whether this relay still holds OS resources after a ``stop`` — an open
        listener, a live forwarded connection, or an unjoined thread. The stack
        teardown asserts this is ``False`` for every attached relay."""
        if self._listener is not None or self._live_conns:
            return True
        if self._accept_thread is not None and self._accept_thread.is_alive():
            return True
        if any(t.is_alive() for t in self._retired_accept_threads):
            return True
        return any(t.is_alive() for t in self._pump_threads)

    # ---- internals -------------------------------------------------------

    def _record_failure(self, exc: BaseException) -> None:
        if self._failure is None:
            self._failure = exc

    def _open_listener(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self._listen_host, self.port))
        listener.listen(128)
        listener.settimeout(0.5)
        self._listener = listener
        self._accept_thread = threading.Thread(target=self._accept_loop, args=(listener,), daemon=True)
        self._accept_thread.start()

    def _is_current_listener(self, listener: socket.socket) -> bool:
        with self._lock:
            return self._listener is listener

    def _fail_listener(self, listener: socket.socket, exc: BaseException) -> None:
        """Record a real accept-loop failure and CLOSE the listener, so clients are
        refused loudly (ECONNREFUSED) rather than queued behind a dead accept thread,
        and ``restore`` re-opens instead of finding a still-bound listener."""
        with self._lock:
            self._record_failure(exc)
            if self._listener is listener:
                listener.close()
                self._listener = None

    def _accept_loop(self, listener: socket.socket) -> None:
        while True:
            if not self._is_current_listener(listener):
                return
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                # The deliberate close (sever/stop) swaps the listener out first,
                # so anything else is a real failure and must not pass as an
                # outage nobody injected.
                if self._is_current_listener(listener):
                    self._fail_listener(listener, exc)
                return
            try:
                upstream = socket.create_connection(self._upstream, timeout=5.0)
            except OSError as exc:
                # The relay cannot reach the store it fronts: that is infra being
                # down, not an injected outage — surface it and stop serving rather
                # than silently refusing this one client while pretending to be up.
                client.close()
                self._fail_listener(listener, exc)
                return
            self._start_pair(client, upstream)

    def _start_pair(self, client: socket.socket, upstream: socket.socket) -> None:
        with self._lock:
            if self._stopped or self._listener is None:
                # Severed between accept and here — do not adopt the connection.
                client.close()
                upstream.close()
                return
            self._live_conns.extend((client, upstream))
        for src, dst in ((client, upstream), (upstream, client)):
            thread = threading.Thread(target=self._pump, args=(src, dst), daemon=True)
            self._pump_threads.append(thread)
            thread.start()

    def _pump(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                try:
                    data = src.recv(65536)
                except OSError:
                    # The peer went away or the socket was closed under us (the
                    # sever). Either way this direction is finished.
                    break
                if not data:
                    break
                try:
                    dst.sendall(data)
                except OSError:
                    break
        finally:
            # Half-close the peer so the other direction unwinds too, then drop
            # only THIS direction's source: the opposite pump still owns the other
            # socket and deregisters it when it unwinds, so a sever in between
            # still finds the pair's live socket to close.
            with contextlib.suppress(OSError):
                dst.shutdown(socket.SHUT_WR)
            with self._lock:
                if src in self._live_conns:
                    self._live_conns.remove(src)
            with contextlib.suppress(OSError):
                src.close()


def wait_relay_ready(relay: TcpRelay, *, deadline: float = 5.0) -> None:
    """Block until the relay's listener accepts a loopback connection, so a test
    only points the SUT at a relay that is actually serving."""

    def probe() -> bool:
        relay.raise_if_failed()
        try:
            with socket.create_connection((relay.listen_host, relay.port), timeout=0.5):
                return True
        except OSError:
            return False

    wait_for(probe, deadline=deadline, message=f"relay never accepted on port {relay.port}")
    relay.raise_if_failed()
