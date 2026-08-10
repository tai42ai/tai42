"""Task-scoped socket routing for the ``proxy`` tool extension.

The active route lives in a :class:`~contextvars.ContextVar`, so a proxied call routes only
its own connections and concurrent routed/unrouted tasks each carry their own. :func:`install_dispatcher`
swaps :class:`RoutingSocket` onto ``socket.socket`` once; each socket then reads ``active_route``
at creation and configures itself, or stays ordinary. The HTTP/HTTPS path tunnels via a
stdlib-only ``CONNECT``; a SOCKS route dispatches to a lazily-built PySocks subclass, keeping
the HTTP path stdlib-only per the ``proxy`` extra.

Propagation follows the asyncio task tree (``asyncio.to_thread`` inherits; ``run_in_executor``
and raw threads do NOT), and the route is captured at socket CREATION — a tool reusing a pooled
keep-alive connection opened outside the routed window is not re-routed.
"""

from __future__ import annotations

import base64
import socket
import ssl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

# Cap on buffered CONNECT response headers; a proxy that never terminates them is refused, not buffered unbounded.
_MAX_PROXY_HEADER_BYTES = 64 * 1024


@dataclass(frozen=True)
class RouteConfig:
    """One parsed proxy route: where to connect and how to negotiate.

    ``connect_address`` is what the socket connects to (an SSRF-validated IP for a
    caller-supplied proxy, else the proxy hostname); ``proxy_host`` keeps the hostname for
    an HTTPS proxy's TLS SNI and certificate verification.
    """

    is_socks: bool
    is_https: bool
    proxy_host: str
    proxy_port: int
    connect_address: str
    username: str | None
    password: str | None
    rdns: bool
    connect_timeout: int
    # PySocks proxy-type constant for a SOCKS route; ``None`` for the HTTP/HTTPS path.
    socks_type: int | None


active_route: ContextVar[RouteConfig | None] = ContextVar("active_route", default=None)


def load_socks():
    """Import PySocks, surfacing the ``proxy`` extra hint when it is absent."""
    try:
        import socks
    except ImportError as exc:
        raise ImportError(
            "SOCKS proxy support needs the PySocks library. Install it with: pip install 'tai42-toolbox[proxy]'"
        ) from exc
    return socks


# Built on first SOCKS route so the HTTP/HTTPS path never imports PySocks.
_routing_socks_socket_cls: type[RoutingSocket] | None = None


class RoutingSocket(socket.socket):
    """The socket class installed on ``socket.socket``.

    At creation each instance reads ``active_route``: an HTTP/HTTPS route tunnels
    through the proxy in :meth:`connect`; a SOCKS route is dispatched by
    :meth:`__new__` to :class:`RoutingSocksSocket`; no route leaves an ordinary
    socket. ``isinstance(sock, socket.socket)`` holds either way.
    """

    def __new__(cls, family: int = -1, type: int = -1, proto: int = -1, fileno: int | None = None):
        route = active_route.get()
        if cls is RoutingSocket and route is not None and route.is_socks:
            cls = _routing_socks_socket()
        # __new__ only allocates; __init__ opens the descriptor from the arguments.
        return super().__new__(cls)  # type: ignore[arg-type]

    def __init__(self, family: int = -1, type: int = -1, proto: int = -1, fileno: int | None = None):
        super().__init__(family, type, proto, fileno)
        self._route = active_route.get()

    def connect(self, address):
        route = self._route
        if route is None:
            super().connect(address)
            return
        self._connect_via_http_proxy(route, address)

    def _connect_via_http_proxy(self, route: RouteConfig, address) -> None:
        """Tunnel to ``address`` through an HTTP/HTTPS proxy with a ``CONNECT`` handshake.

        The TCP connection targets ``route.connect_address``; an HTTPS proxy is TLS-wrapped
        using ``route.proxy_host`` for SNI and cert verification. The whole negotiation runs
        under a single ``route.connect_timeout`` deadline and the buffered header is capped at
        ``_MAX_PROXY_HEADER_BYTES``. On failure the descriptor-owning socket is closed before
        the error propagates; the caller's timeout is restored on an established socket.
        """
        dest_host, dest_port = address
        if not route.rdns:
            dest_host = socket.gethostbyname(dest_host)

        original_timeout = self.gettimeout()
        deadline = time.monotonic() + route.connect_timeout
        sock: socket.socket = self
        try:
            # Each blocking phase runs under the time left until this deadline, bounding the whole negotiation.
            self.settimeout(max(0.0, deadline - time.monotonic()))
            super().connect((route.connect_address, route.proxy_port))
            if route.is_https:
                # Verify the proxy cert using the original hostname (SNI), not the validated IP.
                self.settimeout(max(0.0, deadline - time.monotonic()))
                context = ssl.create_default_context()
                sock = context.wrap_socket(self, server_hostname=route.proxy_host)
            connect_str = f"CONNECT {dest_host}:{dest_port} HTTP/1.1\r\nHost: {dest_host}:{dest_port}\r\n"
            if route.username and route.password:
                auth = f"{route.username}:{route.password}"
                auth_b64 = base64.b64encode(auth.encode()).decode()
                connect_str += f"Proxy-Authorization: Basic {auth_b64}\r\n"
            connect_str += "\r\n"
            sock.sendall(connect_str.encode())
            response = b""
            while True:
                # Bound each recv by the time left; a non-positive remaining makes recv raise promptly.
                sock.settimeout(max(0.0, deadline - time.monotonic()))
                data = sock.recv(4096)
                if not data:
                    raise OSError("Connection closed by proxy")
                response += data
                if b"\r\n\r\n" in response:
                    break
                if len(response) > _MAX_PROXY_HEADER_BYTES:
                    raise OSError(f"Proxy response headers exceeded {_MAX_PROXY_HEADER_BYTES} bytes")
            header = response.split(b"\r\n\r\n")[0]
            status = header.split(b"\r\n")[0]
            parts = status.split()
            if len(parts) < 2:
                raise OSError(f"Malformed proxy CONNECT response: {status!r}")
            try:
                code = int(parts[1])
            except ValueError as exc:
                raise OSError(f"Malformed proxy CONNECT response: {status!r}") from exc
            if code != 200:
                raise OSError(f"Proxy rejected connection: {status!r}")
            if sock is not self:
                # A TLS wrap replaced ``sock`` and took this instance's fd; forward the wrapped
                # socket's I/O onto this instance so callers keep using the object they were handed.
                self.send = sock.send
                self.sendto = sock.sendto
                self.sendall = sock.sendall
                self.recv = sock.recv
                self.recvfrom = sock.recvfrom
                self.recv_into = sock.recv_into if hasattr(sock, "recv_into") else self.recv_into
                self.close = sock.close
                self.makefile = sock.makefile

                def dummy(*args, **kwargs):
                    raise NotImplementedError("Method not supported in proxied socket")

                self.connect = dummy
        except BaseException:
            # Close the descriptor-owning socket on failure so a half-open tunnel never leaks its fd.
            # A failed TLS wrap detaches this instance's fd (``fileno() == -1``); skip closing that.
            if sock.fileno() != -1:
                sock.close()
            raise
        finally:
            # Restore the caller's timeout on the fd-owning socket; a detached instance has none.
            if sock.fileno() != -1:
                sock.settimeout(original_timeout)


def _routing_socks_socket() -> type[RoutingSocket]:
    """Return the lazily-defined SOCKS routing socket class, building it on first use
    (only reachable once a SOCKS route has selected this path, so PySocks is present).
    """
    global _routing_socks_socket_cls
    if _routing_socks_socket_cls is None:
        socks = load_socks()

        class RoutingSocksSocket(socks.socksocket, RoutingSocket):
            """A ``socks.socksocket`` configured per instance from ``active_route``.

            Per-instance ``set_proxy`` leaves PySocks' class-global ``default_proxy`` untouched,
            so concurrent SOCKS routes never collide. The negotiation runs under
            ``route.connect_timeout`` only when the caller left the timeout unset, restoring it
            afterward so a pooled socket keeps no permanent read timeout.
            """

            def __init__(self, family: int = -1, type: int = -1, proto: int = -1, fileno: int | None = None):
                # socksocket validates ``type``, so normalize the ``-1`` sentinels socket.socket accepts first.
                if family == -1:
                    family = socket.AF_INET
                if type == -1:
                    type = socket.SOCK_STREAM
                if proto == -1:
                    proto = 0
                super().__init__(family, type, proto, fileno)  # type: ignore[arg-type]
                route = active_route.get()
                self._route = route
                if route is not None and route.is_socks:
                    self.set_proxy(
                        route.socks_type,
                        route.connect_address,
                        route.proxy_port,
                        rdns=route.rdns,
                        username=route.username,
                        password=route.password,
                    )

            def connect(self, dest_pair, *args, **kwargs):
                route = self._route
                original_timeout = self.gettimeout()
                # PySocks hangs unbounded with no timeout; inject one only when the caller left it unset.
                inject = route is not None and original_timeout in (None, 0.0)
                if inject and route is not None:
                    self.settimeout(route.connect_timeout)
                try:
                    return super().connect(dest_pair, *args, **kwargs)
                finally:
                    if inject:
                        self.settimeout(original_timeout)

        _routing_socks_socket_cls = RoutingSocksSocket
    return _routing_socks_socket_cls


def install_dispatcher() -> None:
    """Install the routing dispatcher on ``socket.socket`` (idempotent).

    One atomic assignment swaps in :class:`RoutingSocket`; with no active route it produces
    ordinary sockets, so installing it is harmless when nothing is routed.
    """
    if socket.socket is not RoutingSocket:
        socket.socket = RoutingSocket  # type: ignore[misc]


@contextmanager
def route(cfg: RouteConfig) -> Iterator[RouteConfig]:
    """Make ``cfg`` the active route for the duration of the block.

    Every socket created inside the block (in this task and tasks that inherit its
    context) routes through ``cfg``; the route is reset on exit, on success and on
    exception alike.
    """
    token = active_route.set(cfg)
    try:
        yield cfg
    finally:
        active_route.reset(token)
