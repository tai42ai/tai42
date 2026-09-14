"""Shared rig for the proxy-extension test modules: the tool factory, route and
settings builders, the guard-enable helper, and the fake CONNECT/TLS/SOCKS proxy
servers the socket-routing tests drive against.
"""

import socket
import ssl
import threading
import time

import pytest
import trustme
from tai42_kit.net import url_guard
from tai42_kit.net.url_guard import UrlGuardSettings

import tai42_toolbox._internal.extensions.proxy_context as proxy_context_module
from tai42_toolbox._internal.extensions.proxy_context import (
    ProxySettings,
)
from tai42_toolbox._internal.extensions.socket_routing import (
    RouteConfig,
)


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
