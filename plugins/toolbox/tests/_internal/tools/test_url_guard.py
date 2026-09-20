"""Tests that the ``request`` tool honours the tai42-kit SSRF guard.

The guard itself (``resolve_and_validate``/``enforce_size`` and its policy) is
unit-tested in tai42-kit; these cases drive it through the ``request`` entrypoint
to prove the curl RESOLVE pin, the streamed size cap, and the no-follow-redirects
behaviour honour it. All cases are deterministic: hosts are IP literals (no
external DNS) or the loopback test server, and the guard policy is injected per
test.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import pytest
from tai42_kit.net import url_guard
from tai42_kit.net.url_guard import UrlGuardError, UrlGuardSettings

from tai42_toolbox.tools.request import request


def _enable(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> UrlGuardSettings:
    settings = UrlGuardSettings(enabled=True, **kwargs)
    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: settings)
    return settings


def test_http_tool_rejects_a_hostless_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    with pytest.raises(UrlGuardError, match="no host"):
        asyncio.run(request("file:///etc/passwd"))


@pytest.mark.usefixtures("curl_app")
def test_http_tool_blocks_internal_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    with pytest.raises(UrlGuardError):
        asyncio.run(request("http://10.0.0.1/"))


@pytest.mark.usefixtures("curl_app")
def test_http_tool_does_not_follow_redirects_under_guard(monkeypatch: pytest.MonkeyPatch, local_server: Any) -> None:
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    local_server.configure(location="http://10.0.0.1/internal")
    result = asyncio.run(request(f"{local_server.base_url}/start"))
    # The 3xx is returned as-is; the internal redirect target is never fetched.
    assert result["status_code"] == 302
    assert local_server.requests == ["GET"]


@pytest.mark.usefixtures("curl_app")
def test_http_tool_streams_and_caps_size(monkeypatch: pytest.MonkeyPatch, local_server: Any) -> None:
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"], max_response_bytes=4)
    local_server.configure(body=b"way too many bytes", content_type="text/plain")
    with pytest.raises(UrlGuardError, match="exceeds max_response_bytes"):
        asyncio.run(request(f"{local_server.base_url}/big"))


@pytest.mark.usefixtures("curl_app")
def test_http_tool_returns_streamed_body(monkeypatch: pytest.MonkeyPatch, local_server: Any) -> None:
    """A small response under the guard returns correct status and text, proving
    the streamed-body serialize path reconstructs the body it read off the wire."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    local_server.configure(body=b"streamed hello", content_type="text/plain")
    result = asyncio.run(request(f"{local_server.base_url}/ok"))
    assert result["status_code"] == 200
    assert result["text"] == "streamed hello"
    assert result["content"] == b"streamed hello"


@pytest.mark.usefixtures("curl_app")
def test_http_tool_reports_streamed_body_metrics(monkeypatch: pytest.MonkeyPatch, local_server: Any) -> None:
    """The streaming path reports full-body transfer metrics, not curl's time-to-first-byte values."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    body = b"x" * 4096
    local_server.configure(body=body, content_type="application/octet-stream")
    result = asyncio.run(request(f"{local_server.base_url}/blob"))
    assert result["download_size"] == len(body)
    assert result["response_size"] >= len(body)
    assert isinstance(result["elapsed"], float)
    assert result["elapsed"] > 0.0


@pytest.mark.usefixtures("curl_app")
def test_http_tool_decodes_body_with_malformed_charset(monkeypatch: pytest.MonkeyPatch, local_server: Any) -> None:
    """A bogus charset label in the Content-Type does not crash the streamed
    decode — it falls back to a safe decode with replacement."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    local_server.configure(body=b"hello", content_type="text/plain; charset=cp-bogus-xyz")
    result = asyncio.run(request(f"{local_server.base_url}/weird"))
    assert result["status_code"] == 200
    assert result["text"] == "hello"


@pytest.mark.usefixtures("curl_app")
def test_http_tool_pins_each_host_in_a_pooled_session(monkeypatch: pytest.MonkeyPatch, local_server: Any) -> None:
    """A pooled (keyed) session reused across two hosts pins each request to its own validated
    address, because the ``--resolve`` mapping is set per request rather than at session creation."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    local_server.configure(body=b"ok", content_type="text/plain")
    port = urlparse(local_server.base_url).port

    async def resolve_to_loopback(host: str) -> str:
        return "127.0.0.1"

    monkeypatch.setattr(url_guard, "resolve_and_validate", resolve_to_loopback)

    async def two_hosts() -> None:
        # Hostnames that do not resolve in DNS: reaching the loopback server at
        # all proves each was pinned to the validated 127.0.0.1.
        first = await request(f"http://host-a.invalid:{port}/", session_key="pooled")
        second = await request(f"http://host-b.invalid:{port}/", session_key="pooled", clear_session_cookies=False)
        assert first["status_code"] == 200
        assert second["status_code"] == 200

    asyncio.run(two_hosts())
    assert local_server.requests == ["GET", "GET"]


@pytest.mark.usefixtures("curl_app")
def test_http_tool_rejects_a_caller_proxy_at_an_internal_address(
    monkeypatch: pytest.MonkeyPatch, local_server: Any
) -> None:
    # curl's native path leaves a caller proxy unguarded, so the guard validates each proxy host
    # and rejects an internal one before any request.
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    with pytest.raises(UrlGuardError):
        asyncio.run(
            request(
                f"{local_server.base_url}/ok",
                session_params={"proxies": {"all": "http://10.0.0.1:8888"}},
            )
        )
    # The proxy was rejected up front; the destination server was never reached.
    assert local_server.requests == []


@pytest.mark.usefixtures("curl_app")
def test_http_tool_rejects_a_caller_proxy_string_at_an_internal_address(
    monkeypatch: pytest.MonkeyPatch, local_server: Any
) -> None:
    # curl's singular ``proxy`` string form is validated too.
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    with pytest.raises(UrlGuardError):
        asyncio.run(
            request(
                f"{local_server.base_url}/ok",
                session_params={"proxy": "http://169.254.169.254:80"},
            )
        )
    assert local_server.requests == []


class _FakeResp:
    """A minimal streamed curl response for the RESOLVE-map capture test."""

    def __init__(self) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.history: list[Any] = []
        self.header_size = 0
        self.charset: str | None = "utf-8"
        self.encoding: str | None = None

    async def aiter_content(self) -> Any:
        yield b"ok"

    async def aclose(self) -> None:
        return None


class _FakeCookies:
    def clear(self) -> None:
        return None


class _FakeSession:
    """Records the ``curl_options`` in effect at request time so a test can assert
    the RESOLVE pins curl would receive."""

    def __init__(self) -> None:
        self.curl_options: dict[Any, Any] = {}
        self.cookies = _FakeCookies()
        self.captured_options: dict[Any, Any] = {}

    async def request(self, url: str, method: str, stream: bool = False, **params: Any) -> _FakeResp:
        self.captured_options = dict(self.curl_options)
        return _FakeResp()


class _FakeApp:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    @property
    def clients(self) -> Any:
        session = self._session

        class _Clients:
            def client_ctx(self, client_cls: Any, session_params: Any = None, *, fresh: bool = False, **kw: Any) -> Any:
                @contextlib.asynccontextmanager
                async def cm() -> AsyncIterator[_FakeSession]:
                    yield session

                return cm()

        return _Clients()


def test_http_tool_pins_a_caller_proxy_to_its_validated_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller-supplied proxy that passes the guard is pinned to its validated address in curl's
    RESOLVE map (closing DNS-rebinding on the proxy hop), so the map carries a proxy pin alongside
    the destination pin."""
    from curl_cffi import CurlOpt

    from tai42_toolbox._internal.tools import http_client

    _enable(monkeypatch)

    async def resolve(host: str) -> str:
        return {"dest.example": "203.0.113.1", "proxy.example": "203.0.113.9"}[host]

    monkeypatch.setattr(url_guard, "resolve_and_validate", resolve)

    session = _FakeSession()
    monkeypatch.setattr(http_client, "tai42_app", _FakeApp(session))

    asyncio.run(
        request(
            "http://dest.example:80/",
            session_params={"proxy": "http://proxy.example:8080"},
        )
    )

    resolve_map = session.captured_options[CurlOpt.RESOLVE]
    # The destination pin and the proxy pin both reach curl's RESOLVE map.
    assert "dest.example:80:203.0.113.1" in resolve_map
    assert "proxy.example:8080:203.0.113.9" in resolve_map


def test_validate_proxies_returns_pins_for_each_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_validate_proxies`` returns a curl ``--resolve`` pin per proxy, taking the
    port from the URL and the scheme default (https 443, socks 1080, else 80)."""
    from tai42_toolbox._internal.tools.http_client import _validate_proxies

    _enable(monkeypatch)

    async def resolve(host: str) -> str:
        return "203.0.113.20"

    monkeypatch.setattr(url_guard, "resolve_and_validate", resolve)

    pins = asyncio.run(
        _validate_proxies(
            {
                "proxies": {"http": "http://p-http.example", "https": "https://p-tls.example"},
                "proxy": "socks5://p-socks.example",
            }
        )
    )
    assert "p-http.example:80:203.0.113.20" in pins
    assert "p-tls.example:443:203.0.113.20" in pins
    assert "p-socks.example:1080:203.0.113.20" in pins


def test_validate_proxies_rejects_a_hostless_proxy_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_toolbox._internal.tools.http_client import _validate_proxies

    _enable(monkeypatch)
    with pytest.raises(UrlGuardError, match="no host"):
        asyncio.run(_validate_proxies({"proxy": "http://:8080"}))


def test_validate_proxies_rejects_an_out_of_range_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """An out-of-range proxy port fails with a domain-specific ``UrlGuardError`` that
    names the host but redacts the userinfo, never leaking the credentials."""
    from tai42_toolbox._internal.tools.http_client import _validate_proxies

    _enable(monkeypatch)
    with pytest.raises(UrlGuardError, match="out-of-range port") as excinfo:
        asyncio.run(_validate_proxies({"proxy": "http://user:secret@proxy.example:99999"}))
    message = str(excinfo.value)
    assert "secret" not in message
    assert "proxy.example" in message


def test_validate_proxies_checks_the_port_before_resolving(monkeypatch: pytest.MonkeyPatch) -> None:
    """The port is validated before any DNS lookup, so an out-of-range port fails
    fast without ``resolve_and_validate`` ever running for that URL."""
    from tai42_toolbox._internal.tools.http_client import _validate_proxies

    _enable(monkeypatch)

    resolved: list[str] = []

    async def spy_resolve(host: str) -> str:
        resolved.append(host)
        return "203.0.113.9"

    monkeypatch.setattr(url_guard, "resolve_and_validate", spy_resolve)

    with pytest.raises(UrlGuardError, match="out-of-range port"):
        asyncio.run(_validate_proxies({"proxy": "http://proxy.example:99999"}))
    assert resolved == []


def test_validate_proxies_hostless_userinfo_url_does_not_leak_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostless proxy URL that carries userinfo is rejected, and the credentials
    are redacted out of the error message."""
    from tai42_toolbox._internal.tools.http_client import _validate_proxies

    _enable(monkeypatch)
    with pytest.raises(UrlGuardError) as excinfo:
        asyncio.run(_validate_proxies({"proxy": "http://user:secret@:8080"}))
    assert "secret" not in str(excinfo.value)


def test_redact_userinfo_masks_credentials() -> None:
    """``_redact_userinfo`` masks ``user:pass@`` credentials, preserving host and path. A URL with
    no userinfo is unchanged, and an ``@`` inside the credentials still splits at the last ``@``."""
    from tai42_toolbox._internal.tools.http_client import _redact_userinfo

    assert _redact_userinfo("http://u:p@h.example:8080/x") == "http://***@h.example:8080/x"
    assert _redact_userinfo("http://h.example:8080/x") == "http://h.example:8080/x"
    assert _redact_userinfo("http://u:p@ss@h.example:8080/x") == "http://***@h.example:8080/x"


def test_resolve_pin_entry_brackets_ipv6() -> None:
    """The ``--resolve`` pin brackets an IPv6 address (its colons would collide with the field
    separators) and leaves an IPv4 address unbracketed."""
    from tai42_toolbox._internal.tools.http_client import _resolve_pin_entry

    assert _resolve_pin_entry("example.com", 443, "203.0.113.5") == "example.com:443:203.0.113.5"
    assert _resolve_pin_entry("example.com", 443, "2001:db8::1") == "example.com:443:[2001:db8::1]"


def test_resolve_pin_entry_brackets_an_ipv6_host() -> None:
    """An IPv6-literal host is bracketed on the host side of the triple too; a plain hostname or
    IPv4 host stays unbracketed."""
    from tai42_toolbox._internal.tools.http_client import _resolve_pin_entry

    assert _resolve_pin_entry("2001:db8::1", 8080, "203.0.113.9") == "[2001:db8::1]:8080:203.0.113.9"
    assert _resolve_pin_entry("2001:db8::1", 8080, "2001:db8::2") == "[2001:db8::1]:8080:[2001:db8::2]"
    assert _resolve_pin_entry("proxy.example", 8080, "203.0.113.9") == "proxy.example:8080:203.0.113.9"


@pytest.mark.usefixtures("curl_app")
def test_http_tool_serializes_concurrent_same_key_pins(monkeypatch: pytest.MonkeyPatch, local_server: Any) -> None:
    """Two concurrent requests sharing a ``session_key`` each pin a different hostname to the
    loopback server. The pooled session shares one RESOLVE mapping, so the per-key lock must
    serialize the [set pin + request + body read] section for both to reach their own target.
    """
    from tai42_toolbox._internal.tools import http_client

    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    local_server.configure(body=b"ok", content_type="text/plain")
    port = urlparse(local_server.base_url).port

    async def resolve_to_loopback(host: str) -> str:
        return "127.0.0.1"

    monkeypatch.setattr(url_guard, "resolve_and_validate", resolve_to_loopback)

    # Record whether the per-key lock is held while each request streams its body; ``enforce_size``
    # runs inside the locked section, so a recorded ``True`` proves it never races a same-key request.
    captured: dict[str, Any] = {}
    real_session_guard = http_client._session_guard

    @contextlib.asynccontextmanager
    async def spy_session_guard(session_key: str) -> AsyncIterator[None]:
        async with real_session_guard(session_key):
            loop = asyncio.get_running_loop()
            captured["lock"] = http_client._session_locks[loop][session_key][0]
            yield

    monkeypatch.setattr(http_client, "_session_guard", spy_session_guard)

    held: list[bool] = []
    real_enforce = url_guard.enforce_size

    def spy_enforce(nbytes: int) -> None:
        held.append(captured["lock"].locked())
        real_enforce(nbytes)

    monkeypatch.setattr(url_guard, "enforce_size", spy_enforce)

    async def two_concurrent() -> None:
        # Hostnames that do not resolve in DNS: reaching the loopback server at all
        # proves each stayed pinned to the validated 127.0.0.1 for its whole call.
        first, second = await asyncio.gather(
            request(f"http://host-a.invalid:{port}/", session_key="pooled"),
            request(f"http://host-b.invalid:{port}/", session_key="pooled", clear_session_cookies=False),
        )
        assert first["status_code"] == 200
        assert second["status_code"] == 200

    asyncio.run(two_concurrent())

    assert local_server.requests == ["GET", "GET"]
    # The per-key lock was held every time a request streamed its body.
    assert held
    assert all(held)
