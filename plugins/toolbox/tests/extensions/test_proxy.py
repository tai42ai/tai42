"""Tests for the ``proxy`` tool extension and its task-scoped routing core."""

import asyncio
import base64
import inspect
import socket
import ssl
import threading
import time
from typing import cast

import pytest
import trustme
from pydantic import ValidationError
from tai42_contract.extensions import ExtensionKind
from tai42_kit.net import url_guard
from tai42_kit.net.url_guard import UrlGuardError, UrlGuardSettings

import tai42_toolbox._internal.extensions.proxy_context as proxy_context_module
import tai42_toolbox.extensions.proxy as proxy_module
from tai42_toolbox._internal.extensions.proxy_context import (
    ProxySettings,
    _select_proxy_url,
    build_route,
)
from tai42_toolbox._internal.extensions.socket_routing import (
    RouteConfig,
    RoutingSocket,
    active_route,
    install_dispatcher,
    route,
)
from tai42_toolbox.extensions.proxy import proxy


@pytest.fixture(autouse=True)
def _restore_socket():
    """Restore ``socket.socket`` after each test so an ``install_dispatcher`` call
    (permanent by design) never leaks the routing class into the next test."""
    original = socket.socket
    yield
    socket.socket = original


def _tool(text: str) -> str:
    return text


def _http_route(
    *,
    host: str = "proxy.example",
    port: int = 8080,
    connect_address: str | None = None,
    is_https: bool = False,
    rdns: bool = True,
    username: str | None = None,
    password: str | None = None,
    connect_timeout: int = 30,
) -> RouteConfig:
    return RouteConfig(
        is_socks=False,
        is_https=is_https,
        proxy_host=host,
        proxy_port=port,
        connect_address=connect_address or host,
        username=username,
        password=password,
        rdns=rdns,
        connect_timeout=connect_timeout,
        socks_type=None,
    )


def _socks_route(
    socks_type: int,
    *,
    host: str = "proxy.example",
    port: int = 1080,
    connect_address: str | None = None,
    rdns: bool = True,
    connect_timeout: int = 30,
) -> RouteConfig:
    return RouteConfig(
        is_socks=True,
        is_https=False,
        proxy_host=host,
        proxy_port=port,
        connect_address=connect_address or host,
        username=None,
        password=None,
        rdns=rdns,
        connect_timeout=connect_timeout,
        socks_type=socks_type,
    )


def _settings(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> ProxySettings:
    settings = ProxySettings(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr(proxy_context_module, "proxy_settings", lambda: settings)
    return settings


def _enable_guard(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> None:
    settings = UrlGuardSettings(enabled=True, **kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: settings)


# --------------------------------------------------------------------------- #
# Loopback proxy servers (drive the CONNECT / SOCKS paths over real sockets).
# --------------------------------------------------------------------------- #


class _FakeConnectProxy:
    """A loopback HTTP ``CONNECT`` proxy: accepts one connection, records the
    request, and replies with a fixed status line."""

    def __init__(self, response: bytes = b"HTTP/1.1 200 Connection established\r\n\r\n") -> None:
        self.response = response
        self.received = b""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        with conn:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.received = data
            conn.sendall(self.response)

    def __enter__(self) -> "_FakeConnectProxy":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._thread.join(timeout=5)
        self._sock.close()


class _ClosingProxy:
    """Accepts a connection, drains the request, then closes without replying."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        with conn:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk

    def __enter__(self) -> "_ClosingProxy":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._thread.join(timeout=5)
        self._sock.close()


class _HangingProxy:
    """Accepts a connection and holds it open without ever answering, so a client
    that set no connect timeout would block forever."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            time.sleep(3)

    def __enter__(self) -> "_HangingProxy":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._thread.join(timeout=5)
        self._sock.close()


class _TlsConnectProxy:
    """A loopback HTTPS ``CONNECT`` proxy: TLS-wraps the accepted connection with a
    caller-supplied certificate, reads the CONNECT request over TLS, and replies 200."""

    def __init__(self, server_cert: trustme.LeafCert) -> None:
        self._server_cert = server_cert
        self.received = b""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._server_cert.configure_cert(server_ctx)
        try:
            tls_conn = server_ctx.wrap_socket(conn, server_side=True)
        except ssl.SSLError:
            # The client aborted the handshake (it rejected the certificate) — the
            # expected outcome of the hostname-mismatch case; nothing to serve.
            conn.close()
            return
        with tls_conn:
            tls_conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = tls_conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.received = data
            tls_conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

    def __enter__(self) -> "_TlsConnectProxy":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._thread.join(timeout=5)
        self._sock.close()


class _TrickleConnectProxy:
    """Sends its 200 CONNECT reply in small timed chunks, so the client's recv loop
    must iterate several times; every chunk lands within the connect deadline."""

    def __init__(self, chunk_size: int = 4, interval: float = 0.02) -> None:
        self._chunk_size = chunk_size
        self._interval = interval
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        with conn:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            reply = b"HTTP/1.1 200 Connection established\r\n\r\n"
            for i in range(0, len(reply), self._chunk_size):
                try:
                    conn.sendall(reply[i : i + self._chunk_size])
                except OSError:
                    return
                time.sleep(self._interval)

    def __enter__(self) -> "_TrickleConnectProxy":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._thread.join(timeout=5)
        self._sock.close()


class _SlowHeaderProxy:
    """Dribbles partial header bytes slowly, never sending the terminator, then holds the
    connection open — each byte faster than one connect timeout, so only the whole-negotiation
    deadline bounds the wait."""

    def __init__(self, interval: float, count: int) -> None:
        self._interval = interval
        self._count = count
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        with conn:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            for _ in range(self._count):
                try:
                    conn.sendall(b"X")
                except OSError:
                    return
                time.sleep(self._interval)
            time.sleep(3)

    def __enter__(self) -> "_SlowHeaderProxy":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._thread.join(timeout=8)
        self._sock.close()


class _OversizedHeaderProxy:
    """Replies with header bytes that never terminate and overflow the header cap."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        with conn:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            try:
                conn.sendall(b"HTTP/1.1 200 OK\r\n" + b"X" * self._size)
            except OSError:
                return
            time.sleep(1)

    def __enter__(self) -> "_OversizedHeaderProxy":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._thread.join(timeout=5)
        self._sock.close()


class _HangingTlsHandshakeProxy:
    """Accepts the TCP connection for an HTTPS proxy but never runs the TLS handshake, then holds
    the socket open, so only the connect deadline can bound the client's ``wrap_socket`` wait."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            time.sleep(4)

    def __enter__(self) -> "_HangingTlsHandshakeProxy":
        self._thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self._thread.join(timeout=6)
        self._sock.close()


# --------------------------------------------------------------------------- #
# Dispatcher install + socket-class dispatch.
# --------------------------------------------------------------------------- #


def test_install_dispatcher_is_idempotent():
    install_dispatcher()
    assert socket.socket is RoutingSocket
    install_dispatcher()
    assert socket.socket is RoutingSocket


def test_no_route_leaves_a_plain_socket():
    install_dispatcher()
    sock = socket.socket()
    try:
        assert type(sock) is RoutingSocket
        assert isinstance(sock, socket.socket)
        assert sock._route is None
    finally:
        sock.close()


def test_plain_socket_connects_directly_when_no_route_is_active():
    # With no active route, connect() is an ordinary direct connection — the
    # dispatcher installed process-wide never touches unrouted traffic.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        install_dispatcher()
        sock = cast(RoutingSocket, socket.socket())
        assert sock._route is None
        try:
            sock.connect(("127.0.0.1", port))
            conn, _ = listener.accept()
            conn.close()
        finally:
            sock.close()
    finally:
        listener.close()


def test_http_route_socket_isinstance_and_carries_config():
    cfg = _http_route()
    with route(cfg):
        sock = RoutingSocket()
    try:
        assert isinstance(sock, socket.socket)
        assert sock._route is cfg
    finally:
        sock.close()


def test_socks_route_dispatches_to_socksocket_with_validated_address():
    import socks

    # The validated address (not the hostname) is handed to PySocks' set_proxy, so
    # PySocks never re-resolves the hostname at connect (which would reopen rebinding).
    cfg = _socks_route(socks.SOCKS5, host="proxy.example", connect_address="203.0.113.5", rdns=True)
    with route(cfg):
        sock = RoutingSocket()
    try:
        assert isinstance(sock, socket.socket)
        assert isinstance(sock, socks.socksocket)
        proxy_type, addr, port, rdns, _user, _pw = sock.proxy
        assert proxy_type == socks.SOCKS5
        assert addr == "203.0.113.5"
        assert port == 1080
        assert rdns is True
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# Task-scoped routing: concurrent calls route independently.
# --------------------------------------------------------------------------- #


def test_unrouted_call_is_not_proxied_during_a_routed_call():
    # While a routed call is in flight, a concurrent unrelated
    # task's socket must NOT be routed — it stays a plain, unrouted socket.
    install_dispatcher()
    cfg = _http_route()
    captured: dict[str, object] = {}

    async def run() -> None:
        routed_created = asyncio.Event()
        unrouted_done = asyncio.Event()

        async def routed() -> None:
            with route(cfg):
                sock = cast(RoutingSocket, socket.socket())
                captured["routed"] = sock._route
                routed_created.set()
                await unrouted_done.wait()
                captured["routed_after"] = sock._route
                sock.close()

        async def unrouted() -> None:
            await routed_created.wait()
            sock = cast(RoutingSocket, socket.socket())
            captured["unrouted"] = sock._route
            sock.close()
            unrouted_done.set()

        await asyncio.gather(routed(), unrouted())

    asyncio.run(run())
    assert captured["routed"] is cfg
    assert captured["unrouted"] is None
    assert captured["routed_after"] is cfg


def test_concurrent_routed_calls_each_keep_their_own_config():
    install_dispatcher()
    cfg_a = _http_route(host="proxy-a")
    cfg_b = _http_route(host="proxy-b")
    captured: dict[str, object] = {}

    async def run() -> None:
        both_created = asyncio.Barrier(2)

        async def routed(tag: str, cfg: RouteConfig) -> None:
            with route(cfg):
                sock = cast(RoutingSocket, socket.socket())
                # Hold both routes open simultaneously before reading either config.
                await both_created.wait()
                captured[tag] = sock._route
                sock.close()

        await asyncio.gather(routed("a", cfg_a), routed("b", cfg_b))

    asyncio.run(run())
    assert captured["a"] is cfg_a
    assert captured["b"] is cfg_b


def test_route_resets_on_success_and_on_exception():
    cfg = _http_route()
    with route(cfg):
        assert active_route.get() is cfg
    assert active_route.get() is None

    with pytest.raises(RuntimeError, match="boom"), route(cfg):
        raise RuntimeError("boom")
    # The route is reset even when the block raised.
    assert active_route.get() is None


def test_nested_route_restores_outer_on_inner_exit():
    # Leaving the inner block restores the OUTER route (not None), so nested proxied calls in one task keep routing.
    outer, inner = _http_route(host="outer"), _http_route(host="inner")
    with route(outer):
        with route(inner):
            assert active_route.get() is inner
        assert active_route.get() is outer
    assert active_route.get() is None


# --------------------------------------------------------------------------- #
# HTTP/HTTPS CONNECT handshake.
# --------------------------------------------------------------------------- #


def test_http_connect_tunnels_and_restores_caller_timeout():
    with _FakeConnectProxy() as server:
        cfg = _http_route(host="the-proxy", connect_address="127.0.0.1", port=server.port)
        with route(cfg):
            sock = RoutingSocket()
        sock.settimeout(12)
        try:
            sock.connect(("example.com", 80))
            # The caller's timeout is restored after the negotiation window.
            assert sock.gettimeout() == 12
        finally:
            sock.close()
    assert b"CONNECT example.com:80 HTTP/1.1" in server.received
    assert b"Proxy-Authorization" not in server.received


def test_http_connect_sends_proxy_authorization_when_credentialed():
    with _FakeConnectProxy() as server:
        cfg = _http_route(connect_address="127.0.0.1", port=server.port, username="user", password="pass")
        with route(cfg):
            sock = RoutingSocket()
        try:
            sock.connect(("example.com", 80))
        finally:
            sock.close()
    expected = base64.b64encode(b"user:pass").decode()
    assert f"Proxy-Authorization: Basic {expected}".encode() in server.received


def test_http_connect_raises_when_proxy_rejects():
    with _FakeConnectProxy(response=b"HTTP/1.1 403 Forbidden\r\n\r\n") as server:
        cfg = _http_route(connect_address="127.0.0.1", port=server.port)
        with route(cfg):
            sock = RoutingSocket()
        try:
            with pytest.raises(OSError, match="Proxy rejected"):
                sock.connect(("example.com", 80))
        finally:
            sock.close()


def test_http_connect_raises_when_proxy_closes_without_response():
    with _ClosingProxy() as server:
        cfg = _http_route(connect_address="127.0.0.1", port=server.port)
        with route(cfg):
            sock = RoutingSocket()
        try:
            with pytest.raises(OSError, match="Connection closed by proxy"):
                sock.connect(("example.com", 80))
        finally:
            sock.close()


def test_http_connect_succeeds_when_reply_arrives_in_multiple_chunks():
    # The proxy dribbles its 200 reply a few bytes at a time; the recv loop must
    # iterate and reassemble the header before the CONNECT completes.
    with _TrickleConnectProxy(chunk_size=4, interval=0.02) as server:
        cfg = _http_route(connect_address="127.0.0.1", port=server.port, connect_timeout=5)
        with route(cfg):
            sock = RoutingSocket()
        try:
            sock.connect(("example.com", 80))
            # The CONNECT completed and the caller can use the tunnel socket.
            assert sock.fileno() != -1
        finally:
            sock.close()


def test_http_connect_deadline_bounds_a_slow_trickle_proxy():
    # A proxy dribbling header bytes forever would reset a per-recv timeout indefinitely; the
    # whole-negotiation deadline bounds the wait to ~connect_timeout instead.
    with _SlowHeaderProxy(interval=0.7, count=10) as server:
        cfg = _http_route(connect_address="127.0.0.1", port=server.port, connect_timeout=1)
        with route(cfg):
            sock = RoutingSocket()
        try:
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                sock.connect(("example.com", 80))
            elapsed = time.monotonic() - start
            # Bounded by the deadline, not by the number of trickle windows.
            assert elapsed < 2 * cfg.connect_timeout
        finally:
            sock.close()


def test_http_connect_rejects_oversized_response_headers():
    with _OversizedHeaderProxy(size=70 * 1024) as server:
        cfg = _http_route(connect_address="127.0.0.1", port=server.port, connect_timeout=5)
        with route(cfg):
            sock = RoutingSocket()
        try:
            with pytest.raises(OSError, match="headers exceeded"):
                sock.connect(("example.com", 80))
        finally:
            sock.close()


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1\r\n\r\n",
        b"HTTP/1.1 OK banana\r\n\r\n",
        b"\r\n\r\n",
    ],
)
def test_http_connect_raises_oserror_on_a_malformed_status_line(response: bytes):
    with _FakeConnectProxy(response=response) as server:
        cfg = _http_route(connect_address="127.0.0.1", port=server.port)
        with route(cfg):
            sock = RoutingSocket()
        try:
            with pytest.raises(OSError, match="Malformed proxy CONNECT response"):
                sock.connect(("example.com", 80))
        finally:
            sock.close()


def test_http_connect_closes_the_socket_when_the_proxy_rejects():
    # A rejected CONNECT closes the descriptor-owning socket before raising, so the
    # failed tunnel never leaks its file descriptor.
    with _FakeConnectProxy(response=b"HTTP/1.1 403 Forbidden\r\n\r\n") as server:
        cfg = _http_route(connect_address="127.0.0.1", port=server.port)
        with route(cfg):
            sock = RoutingSocket()
        try:
            with pytest.raises(OSError, match="Proxy rejected"):
                sock.connect(("example.com", 80))
            assert sock.fileno() == -1
        finally:
            sock.close()


def test_http_connect_resolves_locally_when_rdns_disabled():
    with _FakeConnectProxy() as server:
        cfg = _http_route(connect_address="127.0.0.1", port=server.port, rdns=False)
        with route(cfg):
            sock = RoutingSocket()
        try:
            sock.connect(("localhost", 80))
        finally:
            sock.close()
    resolved = socket.gethostbyname("localhost")
    assert f"CONNECT {resolved}:80 HTTP/1.1".encode() in server.received


def test_http_connect_times_out_on_a_hanging_proxy():
    with _HangingProxy() as server:
        cfg = _http_route(connect_address="127.0.0.1", port=server.port, connect_timeout=1)
        with route(cfg):
            sock = RoutingSocket()
        try:
            with pytest.raises(TimeoutError):
                sock.connect(("example.com", 80))
        finally:
            sock.close()


def test_https_proxy_connects_to_validated_ip_but_verifies_original_hostname(tmp_path, monkeypatch):
    # The TCP connection targets the validated IP while TLS SNI and cert verification use the
    # original hostname; the handshake succeeds only because they use proxy_host, not the IP.
    ca = trustme.CA()
    hostname = "proxy.internal.example"
    server_cert = ca.issue_cert(hostname)
    ca_file = tmp_path / "trustme-ca.pem"
    ca_file.write_bytes(ca.cert_pem.bytes())
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    monkeypatch.setenv("SSL_CERT_DIR", "")

    with _TlsConnectProxy(server_cert) as server:
        cfg = _http_route(host=hostname, connect_address="127.0.0.1", port=server.port, is_https=True)
        with route(cfg):
            sock = RoutingSocket()
        try:
            sock.connect(("example.com", 443))
            # The forwarding block ran (sock was TLS-wrapped): connect() is now a guard.
            with pytest.raises(NotImplementedError, match="not supported in proxied socket"):
                sock.connect(("example.com", 443))
        finally:
            sock.close()
    assert b"CONNECT example.com:443 HTTP/1.1" in server.received


def test_https_proxy_verification_fails_when_hostname_does_not_match(tmp_path, monkeypatch):
    # If the proxy certificate is for a different hostname than proxy_host, TLS
    # verification fails loudly — proving verification binds to the hostname.
    ca = trustme.CA()
    server_cert = ca.issue_cert("someone-else.example")
    ca_file = tmp_path / "trustme-ca.pem"
    ca_file.write_bytes(ca.cert_pem.bytes())
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
    monkeypatch.setenv("SSL_CERT_DIR", "")

    with _TlsConnectProxy(server_cert) as server:
        cfg = _http_route(host="proxy.internal.example", connect_address="127.0.0.1", port=server.port, is_https=True)
        with route(cfg):
            sock = RoutingSocket()
        try:
            with pytest.raises(ssl.SSLCertVerificationError):
                sock.connect(("example.com", 443))
        finally:
            sock.close()


def test_https_proxy_connect_bounds_a_hanging_tls_handshake():
    # The TLS handshake runs under the same whole-negotiation deadline as the connect; a proxy
    # that never runs the handshake would block ``wrap_socket`` forever without it.
    with _HangingTlsHandshakeProxy() as server:
        cfg = _http_route(connect_address="127.0.0.1", port=server.port, is_https=True, connect_timeout=1)
        with route(cfg):
            sock = RoutingSocket()
        try:
            start = time.monotonic()
            with pytest.raises(TimeoutError):
                sock.connect(("example.com", 443))
            elapsed = time.monotonic() - start
            # Bounded by the one deadline, not left to hang on the TLS handshake.
            assert elapsed < 2 * cfg.connect_timeout
        finally:
            sock.close()


# --------------------------------------------------------------------------- #
# SOCKS negotiation timeout.
# --------------------------------------------------------------------------- #


def test_socks_connect_times_out_on_a_hanging_proxy():
    import socks

    with _HangingProxy() as server:
        cfg = _socks_route(socks.SOCKS5, connect_address="127.0.0.1", port=server.port, connect_timeout=1)
        with route(cfg):
            sock = RoutingSocket()
        try:
            # No caller timeout is set, so the negotiation timeout is injected;
            # PySocks wraps the expiry as a ProxyError (an OSError subclass).
            with pytest.raises(socks.ProxyError):
                sock.connect(("example.com", 80))
        finally:
            sock.close()


def test_socks_injects_negotiation_timeout_and_restores_it(monkeypatch):
    import socks

    seen: dict[str, object] = {}

    def fake_connect(self, dest_pair, *args, **kwargs):
        seen["during"] = self.gettimeout()

    monkeypatch.setattr(socks.socksocket, "connect", fake_connect)

    cfg = _socks_route(socks.SOCKS5, connect_address="203.0.113.5", connect_timeout=7)
    with route(cfg):
        sock = RoutingSocket()
    try:
        assert sock.gettimeout() in (None, 0.0)
        sock.connect(("example.com", 80))
        # The negotiation ran under the injected timeout; the caller's unset timeout
        # is restored so it is never left as a permanent read timeout.
        assert seen["during"] == 7
        assert sock.gettimeout() is None
    finally:
        sock.close()


def test_socks_leaves_a_caller_set_timeout_untouched(monkeypatch):
    import socks

    seen: dict[str, object] = {}

    def fake_connect(self, dest_pair, *args, **kwargs):
        seen["during"] = self.gettimeout()

    monkeypatch.setattr(socks.socksocket, "connect", fake_connect)

    cfg = _socks_route(socks.SOCKS5, connect_address="203.0.113.5", connect_timeout=7)
    with route(cfg):
        sock = RoutingSocket()
    try:
        sock.settimeout(5)
        sock.connect(("example.com", 80))
        # A caller-set timeout is honored, not overridden by the negotiation timeout.
        assert seen["during"] == 5
        assert sock.gettimeout() == 5
    finally:
        sock.close()


# --------------------------------------------------------------------------- #
# Config parse + pool policy (proxy_context.build_route / _select_proxy_url).
# --------------------------------------------------------------------------- #


def test_build_route_rotates_over_the_operator_pool_when_omitted(monkeypatch):
    _settings(monkeypatch, pool=["http://pool-proxy.example:8080"])
    cfg = asyncio.run(build_route(None))
    assert cfg.proxy_host == "pool-proxy.example"
    assert cfg.proxy_port == 8080
    assert cfg.is_socks is False


def test_build_route_accepts_a_pool_selection(monkeypatch):
    _settings(monkeypatch, pool=["http://a.example:8080", "http://b.example:8080"])
    cfg = asyncio.run(build_route(["http://a.example:8080"]))
    assert cfg.proxy_host == "a.example"


def test_build_route_rejects_an_out_of_pool_url_by_default(monkeypatch):
    _settings(monkeypatch, pool=["http://trusted.example:8080"])
    with pytest.raises(ValueError, match="not in the operator pool"):
        asyncio.run(build_route(["http://attacker.example:8080"]))


def test_build_route_accepts_a_caller_url_when_allowed(monkeypatch):
    _settings(monkeypatch, pool=[], allow_caller_urls=True)
    cfg = asyncio.run(build_route(["http://caller.example:8080"]))
    assert cfg.proxy_host == "caller.example"


def test_build_route_raises_when_no_proxies_available(monkeypatch):
    _settings(monkeypatch, pool=[])
    with pytest.raises(ValueError, match="No proxies available"):
        asyncio.run(build_route(None))


def test_build_route_parses_a_socks4_url(monkeypatch):
    import socks

    _settings(monkeypatch, pool=["socks4a://proxy.example:1080"])
    cfg = asyncio.run(build_route(None))
    assert cfg.is_socks is True
    assert cfg.socks_type == socks.SOCKS4
    assert cfg.rdns is True


def test_build_route_rejects_a_hostless_proxy_url(monkeypatch):
    _settings(monkeypatch, pool=[], allow_caller_urls=True)
    with pytest.raises(ValueError, match="has no host"):
        asyncio.run(build_route(["http://:8080"]))


def test_build_route_rejects_an_unsupported_scheme(monkeypatch):
    _settings(monkeypatch, pool=[], allow_caller_urls=True)
    with pytest.raises(ValueError, match="Unsupported proxy scheme"):
        asyncio.run(build_route(["ftp://host.example:21"]))


def test_build_route_rejects_an_out_of_range_port(monkeypatch):
    _settings(monkeypatch, pool=[], allow_caller_urls=True)
    with pytest.raises(ValueError, match="out-of-range port") as excinfo:
        asyncio.run(build_route(["http://host.example:99999"]))
    # The domain-specific error names the offending proxy, not a bare stdlib message.
    assert "host.example" in str(excinfo.value)


def test_build_route_out_of_pool_error_redacts_userinfo(monkeypatch):
    _settings(monkeypatch, pool=["http://trusted.example:8080"])
    with pytest.raises(ValueError, match="not in the operator pool") as excinfo:
        asyncio.run(build_route(["http://alice:s3cr3t-pw@attacker.example:8080"]))
    message = str(excinfo.value)
    assert "s3cr3t-pw" not in message
    assert "***@attacker.example:8080" in message


def test_build_route_hostless_error_redacts_userinfo(monkeypatch):
    _settings(monkeypatch, pool=[], allow_caller_urls=True)
    with pytest.raises(ValueError, match="has no host") as excinfo:
        asyncio.run(build_route(["http://alice:s3cr3t-pw@:8080"]))
    assert "s3cr3t-pw" not in str(excinfo.value)


def test_build_route_socks_missing_extra_raises_install_hint(monkeypatch):
    import sys

    _settings(monkeypatch, pool=["socks5://proxy.example:1080"])
    monkeypatch.setitem(sys.modules, "socks", None)
    with pytest.raises(ImportError, match=r"tai42-toolbox\[proxy\]"):
        asyncio.run(build_route(None))


def test_select_proxy_url_treats_empty_list_as_pool_rotation(monkeypatch):
    settings = ProxySettings(pool=["http://pool.example:8080"])
    assert _select_proxy_url([], settings) == "http://pool.example:8080"


def test_select_proxy_url_rotates_over_the_full_candidate_pool(monkeypatch):
    # Selection hands random.choice the whole candidate list so rotation can reach any entry.
    pool = ["http://a.example:8080", "http://b.example:8080", "http://c.example:8080"]
    settings = ProxySettings(pool=pool)
    seen: dict[str, object] = {}

    def fake_choice(candidates):
        seen["candidates"] = list(candidates)
        return candidates[-1]

    monkeypatch.setattr(proxy_context_module.random, "choice", fake_choice)

    chosen = _select_proxy_url(None, settings)
    assert seen["candidates"] == pool
    assert chosen == "http://c.example:8080"


# --------------------------------------------------------------------------- #
# SSRF guard: caller-supplied hosts guarded, operator pool trusted.
# --------------------------------------------------------------------------- #


def test_operator_pool_host_is_not_guarded(monkeypatch):
    # A private-IP pool entry (a corporate egress proxy) still routes with the guard
    # ON: the pool is trusted-by-configuration and resolve_and_validate is never called.
    _settings(monkeypatch, pool=["http://10.0.0.1:8080"])
    _enable_guard(monkeypatch)

    async def fail_if_called(host: str) -> str:
        raise AssertionError("operator pool host must not be validated")

    monkeypatch.setattr(url_guard, "resolve_and_validate", fail_if_called)

    cfg = asyncio.run(build_route(None))
    assert cfg.connect_address == "10.0.0.1"
    assert cfg.proxy_host == "10.0.0.1"


def test_caller_http_proxy_host_is_validated_and_connects_to_validated_ip(monkeypatch):
    _settings(monkeypatch, pool=[], allow_caller_urls=True)
    _enable_guard(monkeypatch)

    async def resolve(host: str) -> str:
        assert host == "caller-proxy.example"
        return "203.0.113.9"

    monkeypatch.setattr(url_guard, "resolve_and_validate", resolve)

    cfg = asyncio.run(build_route(["https://caller-proxy.example:8443"]))
    # The validated IP is the connect target; the hostname stays for TLS SNI/verify.
    assert cfg.connect_address == "203.0.113.9"
    assert cfg.proxy_host == "caller-proxy.example"
    assert cfg.is_https is True


def test_caller_socks_proxy_host_receives_the_validated_address(monkeypatch):
    _settings(monkeypatch, pool=[], allow_caller_urls=True)
    _enable_guard(monkeypatch)

    async def resolve(host: str) -> str:
        return "203.0.113.9"

    monkeypatch.setattr(url_guard, "resolve_and_validate", resolve)

    cfg = asyncio.run(build_route(["socks5h://caller-proxy.example:1080"]))
    assert cfg.connect_address == "203.0.113.9"
    assert cfg.proxy_host == "caller-proxy.example"
    assert cfg.is_socks is True


def test_caller_http_proxy_to_internal_address_is_rejected(monkeypatch):
    _settings(monkeypatch, pool=[], allow_caller_urls=True)
    _enable_guard(monkeypatch)
    with pytest.raises(UrlGuardError):
        asyncio.run(build_route(["http://127.0.0.1:8080"]))


def test_caller_socks_proxy_to_internal_address_is_rejected(monkeypatch):
    _settings(monkeypatch, pool=[], allow_caller_urls=True)
    _enable_guard(monkeypatch)
    with pytest.raises(UrlGuardError):
        asyncio.run(build_route(["socks5://127.0.0.1:1080"]))


def test_caller_proxy_not_guarded_when_guard_disabled(monkeypatch):
    # The root conftest disables the guard by default. A caller URL at loopback then
    # parses without validation (the operator opt-out is the existing behavior).
    _settings(monkeypatch, pool=[], allow_caller_urls=True)
    cfg = asyncio.run(build_route(["http://127.0.0.1:8080"]))
    assert cfg.connect_address == "127.0.0.1"


# --------------------------------------------------------------------------- #
# ProxySettings env.
# --------------------------------------------------------------------------- #


def test_proxy_settings_returns_a_proxy_settings_instance():
    from tai42_toolbox._internal.extensions.proxy_context import proxy_settings

    assert isinstance(proxy_settings(), ProxySettings)


def test_pool_reads_the_prefixed_env(monkeypatch):
    monkeypatch.setenv("PROXY_POOL", '["http://from-env.example:8080"]')
    assert ProxySettings().pool == ["http://from-env.example:8080"]


def test_bare_unprefixed_proxies_env_is_ignored(monkeypatch):
    # An unprefixed PROXIES env is not read; the pool reads only the PROXY_-prefixed
    # PROXY_POOL, so an ambient PROXIES value leaves the pool empty.
    monkeypatch.setenv("PROXIES", '["http://ambient.example:9999"]')
    assert ProxySettings().pool == []


def test_connect_timeout_and_allow_caller_urls_read_prefixed_env(monkeypatch):
    monkeypatch.setenv("PROXY_CONNECT_TIMEOUT", "7")
    monkeypatch.setenv("PROXY_ALLOW_CALLER_URLS", "true")
    settings = ProxySettings()
    assert settings.connect_timeout == 7
    assert settings.allow_caller_urls is True


@pytest.mark.parametrize("value", ["0", "-1"])
def test_connect_timeout_rejects_non_positive_values(monkeypatch, value):
    # A non-positive timeout would silently break every proxied connect; it must fail at construction.
    monkeypatch.setenv("PROXY_CONNECT_TIMEOUT", value)
    with pytest.raises(ValidationError):
        ProxySettings()


# --------------------------------------------------------------------------- #
# The proxy extension factory.
# --------------------------------------------------------------------------- #


def test_registers_as_wrapper_named_proxy_requiring_body_locality(capture_registration):
    # ``requires_body_locality=True``: routing works only in the process running the tool body,
    # so the bind engine must place it inside any execution-relocating extension.
    assert capture_registration(proxy_module) == [("proxy", ExtensionKind.WRAPPER, True)]


def test_reserved_params_value():
    assert proxy.reserved_params == frozenset({"proxies"})


def test_factory_installs_the_dispatcher():
    proxy(_tool, "tool", "desc")
    assert socket.socket is RoutingSocket


def test_composed_signature_is_original_plus_proxies():
    original = inspect.signature(_tool)
    composed = inspect.signature(proxy(_tool, "tool", "desc"))

    original_names = set(original.parameters)
    composed_names = set(composed.parameters)

    assert original_names <= composed_names
    assert composed_names - original_names == {"proxies"}
    assert composed.parameters["proxies"].kind is inspect.Parameter.KEYWORD_ONLY
    assert composed.parameters["text"].annotation is str


def test_wraps_a_tool_ending_in_var_keyword():
    def tool(text: str, **kwargs: object) -> str:
        return text

    composed = inspect.signature(proxy(tool, "tool", "desc"))
    names = list(composed.parameters)

    assert names[-1] == "kwargs"
    assert composed.parameters["proxies"].kind is inspect.Parameter.KEYWORD_ONLY


def test_wrapper_runs_tool_with_route_active_and_resets_after(monkeypatch):
    _settings(monkeypatch, pool=["http://pool-proxy.example:8080"])
    captured: dict[str, object] = {}

    async def tool(text: str) -> str:
        captured["route"] = active_route.get()
        return text

    wrapped = proxy(tool, "tool", "desc")
    result = asyncio.run(wrapped("hi"))

    assert result == "hi"
    assert isinstance(captured["route"], RouteConfig)
    assert captured["route"].proxy_host == "pool-proxy.example"
    # The route is reset once the call returns.
    assert active_route.get() is None


def test_wrapper_raises_out_of_pool_url_and_routes_nothing(monkeypatch):
    _settings(monkeypatch, pool=["http://trusted.example:8080"])
    ran = {"tool": False}

    async def tool(text: str) -> str:
        ran["tool"] = True
        return text

    wrapped = proxy(tool, "tool", "desc")
    with pytest.raises(ValueError, match="not in the operator pool"):
        asyncio.run(wrapped("hi", proxies=["http://attacker.example:8080"]))
    assert ran["tool"] is False


def test_wrapper_supports_sync_tools(monkeypatch):
    _settings(monkeypatch, pool=["http://pool-proxy.example:8080"])

    def tool(text: str) -> str:
        return text.upper()

    wrapped = proxy(tool, "tool", "desc")
    assert asyncio.run(wrapped("hi")) == "HI"
