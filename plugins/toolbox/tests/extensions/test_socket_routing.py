"""Socket routing: HTTP/HTTPS/SOCKS CONNECT tunnelling, route building, proxy
selection over the pool, and the proxy settings/env reading.
"""

import asyncio
import base64
import socket
import ssl
import time

import pytest
import trustme
from pydantic import ValidationError
from tai42_kit.net import url_guard
from tai42_kit.net.url_guard import UrlGuardError
from tests.extensions._proxy_support import (
    _ClosingProxy,
    _enable_guard,
    _FakeConnectProxy,
    _HangingProxy,
    _HangingTlsHandshakeProxy,
    _http_route,
    _OversizedHeaderProxy,
    _settings,
    _SlowHeaderProxy,
    _socks_route,
    _TlsConnectProxy,
    _TrickleConnectProxy,
)

import tai42_toolbox._internal.extensions.proxy_context as proxy_context_module
from tai42_toolbox._internal.extensions.proxy_context import (
    ProxySettings,
    _select_proxy_url,
    build_route,
)
from tai42_toolbox._internal.extensions.socket_routing import (
    RoutingSocket,
    route,
)


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
