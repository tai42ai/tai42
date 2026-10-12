"""Tests for ``guarded_request``: the SSRF-guarded GET/POST with no redirects, a size cap and one total deadline.

Driven against a real loopback server (the pinning transport connects to a validated address, so an
httpx-level mock cannot reach it). Guard policy is injected per test.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from tai42_kit.net import (
    GuardedRequestError,
    GuardedRequestTimeoutError,
    GuardedResponse,
    guarded_request,
    is_loopback_host,
    url_guard,
)
from tai42_kit.net.url_guard import UrlGuardError, UrlGuardSettings


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _route(self) -> None:
        server: Any = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        server.requests.append(
            {"method": self.command, "path": self.path, "body": body, "content_type": self.headers.get("Content-Type")}
        )
        mode = server.mode
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "drip":
            self.send_response(200)
            self.send_header("Content-Length", "20")
            self.end_headers()
            self.wfile.flush()
            for _ in range(20):
                time.sleep(0.2)
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    return
            return
        if mode == "silent":
            time.sleep(3)
            return
        payload: bytes = server.payload
        self.send_response(server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Echo", "yes")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _route
    do_POST = _route
    do_DELETE = _route

    def log_message(self, *args: Any) -> None:
        pass


class _Server:
    def __init__(self, server: Any) -> None:
        self._server = server
        self.base_url = f"http://127.0.0.1:{server.server_address[1]}"

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._server.requests

    def set(self, *, mode: str = "plain", payload: bytes = b"{}", status: int = 200) -> None:
        self._server.mode = mode
        self._server.payload = payload
        self._server.status = status


@pytest.fixture
def server() -> Iterator[_Server]:
    srv: Any = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.requests = []
    srv.mode = "plain"
    srv.payload = b"{}"
    srv.status = 200
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield _Server(srv)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _enable(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    settings = UrlGuardSettings(enabled=True, **kwargs)
    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: settings)


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: UrlGuardSettings(enabled=False))


def _allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])


def _chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__ or current.__context__
    return "\n".join(parts)


@pytest.mark.parametrize("url", ["https://10.0.0.1/x", "https://192.168.1.10/x", "https://169.254.169.254/latest"])
async def test_private_and_link_local_targets_are_refused(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    _enable(monkeypatch)
    with pytest.raises(UrlGuardError):
        await guarded_request("GET", url, timeout=2, max_bytes=1024)


async def test_loopback_is_refused_unless_an_allowed_range_admits_it(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    _enable(monkeypatch)
    with pytest.raises(UrlGuardError):
        await guarded_request("GET", f"{server.base_url}/doc", timeout=2, max_bytes=1024)
    assert server.requests == []
    _allow_loopback(monkeypatch)
    response = await guarded_request("GET", f"{server.base_url}/doc", timeout=2, max_bytes=1024)
    assert isinstance(response, GuardedResponse)
    assert response.status == 200
    assert response.body == b"{}"
    assert response.headers["x-echo"] == "yes"


@pytest.mark.parametrize("guard_on", [True, False])
async def test_http_on_a_non_loopback_host_is_refused(monkeypatch: pytest.MonkeyPatch, guard_on: bool) -> None:
    if guard_on:
        _enable(monkeypatch)
    else:
        _disable(monkeypatch)
    with pytest.raises(UrlGuardError, match="https"):
        await guarded_request("GET", "http://auth.example.com/token", timeout=2, max_bytes=1024)


async def test_a_redirect_is_returned_not_followed(monkeypatch: pytest.MonkeyPatch, server: _Server) -> None:
    _allow_loopback(monkeypatch)
    server.set(mode="redirect")
    response = await guarded_request("GET", f"{server.base_url}/start", timeout=2, max_bytes=1024)
    assert response.status == 302
    assert response.headers["location"] == "/elsewhere"
    assert [r["path"] for r in server.requests] == ["/start"]


@pytest.mark.parametrize("guard_on", [True, False])
async def test_the_size_cap_raises(monkeypatch: pytest.MonkeyPatch, server: _Server, guard_on: bool) -> None:
    if guard_on:
        _allow_loopback(monkeypatch)
    else:
        _disable(monkeypatch)
    server.set(payload=b"y" * 5000)
    with pytest.raises(GuardedRequestError, match="1000"):
        await guarded_request("GET", f"{server.base_url}/big", timeout=2, max_bytes=1000)


async def test_a_dripping_server_is_cut_off_at_the_total_deadline(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    _allow_loopback(monkeypatch)
    server.set(mode="drip")
    started = time.monotonic()
    with pytest.raises(GuardedRequestTimeoutError):
        await guarded_request("GET", f"{server.base_url}/drip", timeout=0.6, max_bytes=1024)
    elapsed = time.monotonic() - started
    assert 0.5 <= elapsed < 2.0


async def test_a_silent_server_gives_the_same_timeout_error(monkeypatch: pytest.MonkeyPatch, server: _Server) -> None:
    _allow_loopback(monkeypatch)
    server.set(mode="silent")
    started = time.monotonic()
    with pytest.raises(GuardedRequestTimeoutError):
        await guarded_request("GET", f"{server.base_url}/silent", timeout=0.4, max_bytes=1024)
    assert time.monotonic() - started < 2.0


async def test_post_form_and_json_bodies_arrive(monkeypatch: pytest.MonkeyPatch, server: _Server) -> None:
    _allow_loopback(monkeypatch)
    await guarded_request(
        "POST", f"{server.base_url}/form", form={"grant_type": "refresh_token", "a": "b c"}, timeout=2, max_bytes=1024
    )
    await guarded_request(
        "POST", f"{server.base_url}/json", json_body={"jsonrpc": "2.0", "id": 1}, timeout=2, max_bytes=1024
    )
    form, body = server.requests
    assert form["body"] == b"grant_type=refresh_token&a=b+c"
    assert form["content_type"] == "application/x-www-form-urlencoded"
    assert json.loads(body["body"]) == {"jsonrpc": "2.0", "id": 1}
    assert body["content_type"] == "application/json"


async def test_request_headers_are_sent_and_delete_is_supported(
    monkeypatch: pytest.MonkeyPatch, server: _Server
) -> None:
    _allow_loopback(monkeypatch)
    response = await guarded_request(
        "DELETE", f"{server.base_url}/session", headers={"Mcp-Session-Id": "s-1"}, timeout=2, max_bytes=1024
    )
    assert response.status == 200
    assert server.requests[0]["method"] == "DELETE"


async def test_form_and_json_together_are_refused() -> None:
    with pytest.raises(ValueError, match="form or json_body"):
        await guarded_request("POST", "https://a.example.com", form={"a": "b"}, json_body={}, timeout=1, max_bytes=1)


async def test_guard_off_keeps_loopback_http_working(monkeypatch: pytest.MonkeyPatch, server: _Server) -> None:
    _disable(monkeypatch)
    response = await guarded_request("GET", f"{server.base_url}/doc", timeout=2, max_bytes=1024)
    assert response.status == 200


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_error_text_carries_neither_the_query_nor_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_loopback(monkeypatch)
    url = f"http://127.0.0.1:{_closed_port()}/token?code=query-secret"
    with pytest.raises(GuardedRequestError) as info:
        await guarded_request("POST", url, form={"client_secret": "body-secret"}, timeout=2, max_bytes=1024)
    text = _chain_text(info.value)
    assert "query-secret" not in text
    assert "body-secret" not in text
    assert "127.0.0.1" in str(info.value)


async def test_refusal_and_timeout_text_carry_no_query(monkeypatch: pytest.MonkeyPatch, server: _Server) -> None:
    _allow_loopback(monkeypatch)
    with pytest.raises(UrlGuardError) as refused:
        await guarded_request("GET", "http://auth.example.com/x?code=query-secret", timeout=1, max_bytes=1)
    assert "query-secret" not in _chain_text(refused.value)
    server.set(mode="silent")
    with pytest.raises(GuardedRequestTimeoutError) as timed_out:
        await guarded_request("GET", f"{server.base_url}/x?code=query-secret", timeout=0.3, max_bytes=1024)
    assert "query-secret" not in _chain_text(timed_out.value)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("127.8.0.1", True),
        ("::1", True),
        ("example.com", False),
        ("10.0.0.1", False),
    ],
)
def test_is_loopback_host(host: str, expected: bool) -> None:
    assert is_loopback_host(host) is expected


def test_the_package_exports_the_functions_not_their_modules() -> None:
    import inspect

    import tai42_kit.net as net

    assert inspect.isfunction(net.guarded_request)
    assert inspect.isfunction(net.fetch_url)
