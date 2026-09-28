"""Tests for ``fetch_media``: the SSRF-pinned, credentialed, size-capped streaming
fetch for vendor media that returns ``(bytes, Content-Type)``.

Like ``fetch_url``, the download is exercised end-to-end against real loopback servers
(the pinning transport connects to a validated address, so an httpx-level mock cannot
reach it). Guard policy is injected per test; a second loopback server gives a redirect a
distinct origin (a different port) to cross.
"""

from __future__ import annotations

import http.server
import logging
import subprocess
import sys
import threading
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from tai42_kit.logging.logger import _HttpxRequestLineRedactor, setup_logging
from tai42_kit.logging.settings import LoggingSettings
from tai42_kit.net import MediaFetchError, MediaStream, fetch_media, open_media_stream, url_guard
from tai42_kit.net.fetch_media import _content_length, _media_type
from tai42_kit.net.request_body import RequestBodyTooLargeError
from tai42_kit.net.url_guard import UrlGuardError, UrlGuardSettings


def _enable(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> UrlGuardSettings:
    settings = UrlGuardSettings(enabled=True, **kwargs)
    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: settings)
    return settings


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: UrlGuardSettings(enabled=False))


class _MediaHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # http.server dispatch name
        server: Any = self.server
        server.records.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "x-vendor-token": self.headers.get("X-Vendor-Token"),
            }
        )
        redirect = server.redirects.get(self.path)
        if redirect is not None:
            status, location = redirect
            self.send_response(status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        config = server.config
        body: bytes = config["body"]
        self.send_response(config["status"])
        if config["content_type"] is not None:
            self.send_header("Content-Type", config["content_type"])
        if config["include_content_length"]:
            override = config["content_length_override"]
            self.send_header("Content-Length", override if override is not None else str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence per-request logging
        pass


class _MediaServer:
    def __init__(self, base_url: str, server: http.server.HTTPServer) -> None:
        self.base_url = base_url
        self._server = server

    @property
    def records(self) -> list[dict[str, Any]]:
        return self._server.records  # type: ignore[attr-defined]

    def configure(
        self,
        *,
        body: bytes = b"",
        status: int = 200,
        content_type: str | None = "application/octet-stream",
        include_content_length: bool = True,
        content_length_override: str | None = None,
    ) -> None:
        self._server.config = {  # type: ignore[attr-defined]
            "body": body,
            "status": status,
            "content_type": content_type,
            "include_content_length": include_content_length,
            "content_length_override": content_length_override,
        }

    def redirect(self, from_path: str, to_location: str, *, status: int = 307) -> None:
        self._server.redirects[from_path] = (status, to_location)  # type: ignore[attr-defined]


def _serve() -> Iterator[_MediaServer]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _MediaHandler)
    port = server.server_address[1]
    server.config = {  # type: ignore[attr-defined]
        "body": b"",
        "status": 200,
        "content_type": "application/octet-stream",
        "include_content_length": True,
        "content_length_override": None,
    }
    server.redirects = {}  # type: ignore[attr-defined]
    server.records = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _MediaServer(f"http://127.0.0.1:{port}", server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def media_server() -> Iterator[_MediaServer]:
    yield from _serve()


@pytest.fixture
def media_server_2() -> Iterator[_MediaServer]:
    """A second loopback server, so a redirect crosses to a different origin (port)."""
    yield from _serve()


class _CaptureHandler(logging.Handler):
    def __init__(self, sink: list[logging.LogRecord]) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.append(record)


@pytest.fixture
def installed_logging() -> Iterator[None]:
    """Install the platform logging setup (which fits the httpx redactor) and restore after.

    ``setup_logging`` runs ``basicConfig(force=True)``, replacing the root handlers, so the
    root and the httpx logger are both snapshotted and restored.
    """
    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    saved_root_handlers, saved_root_level = root.handlers[:], root.level
    saved_httpx_filters = httpx_logger.filters[:]
    setup_logging(LoggingSettings(log_level="INFO"))
    try:
        yield
    finally:
        root.handlers[:] = saved_root_handlers
        root.setLevel(saved_root_level)
        httpx_logger.filters[:] = saved_httpx_filters


async def test_fetch_media_streams_within_cap(monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer) -> None:
    """A body under the cap returns ``(bytes, Content-Type)`` through the pinning transport."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.configure(body=b"hello media", content_type="image/png")
    data, mime = await fetch_media(f"{media_server.base_url}/obj", max_bytes=1024)
    assert data == b"hello media"
    assert mime == "image/png"
    assert [r["path"] for r in media_server.records] == ["/obj"]


async def test_fetch_media_without_guard_skips_the_pinning_transport(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer
) -> None:
    """With the guard disabled a loopback URL is fetched though loopback is not opted in — no pin runs.

    Under the enabled guard the same URL (127.0.0.1, absent from ``allow_cidrs``) is blocked by the
    pinning transport (see ``test_fetch_media_ssrf_blocks_private_target``); a success here proves the
    pin is skipped, honouring the one operator switch ``fetch_url`` honours.
    """
    _disable(monkeypatch)
    media_server.configure(body=b"unpinned", content_type="image/png")
    data, mime = await fetch_media(f"{media_server.base_url}/obj", max_bytes=1024)
    assert data == b"unpinned"
    assert mime == "image/png"
    assert [r["path"] for r in media_server.records] == ["/obj"]


async def test_fetch_media_over_cap_raises_not_truncates(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer
) -> None:
    """A body over ``max_bytes`` raises mid-stream and is never returned truncated."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.configure(body=b"x" * 64, content_type="image/png")
    with pytest.raises(RequestBodyTooLargeError, match="exceeds the 8-byte cap"):
        await fetch_media(f"{media_server.base_url}/big", max_bytes=8)


async def test_fetch_media_passes_auth_and_headers(monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer) -> None:
    """A caller ``auth`` tuple is applied as the request's ``Authorization`` header."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.configure(body=b"ok", content_type="application/pdf")
    data, _ = await fetch_media(f"{media_server.base_url}/obj", auth=("user", "pass"), max_bytes=1024)
    assert data == b"ok"
    # base64("user:pass") == dXNlcjpwYXNz
    assert media_server.records[0]["authorization"] == "Basic dXNlcjpwYXNz"


async def test_fetch_media_passes_explicit_authorization_header(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer
) -> None:
    """A caller ``Authorization`` header rides the request on a same-origin fetch."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.configure(body=b"ok", content_type="application/pdf")
    await fetch_media(f"{media_server.base_url}/obj", headers={"Authorization": "Bearer TOK"}, max_bytes=1024)
    assert media_server.records[0]["authorization"] == "Bearer TOK"


async def test_fetch_media_follow_redirects(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer, media_server_2: _MediaServer
) -> None:
    """A 307 to a second host is followed under the pinning transport when enabled."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.redirect("/start", f"{media_server_2.base_url}/final")
    media_server_2.configure(body=b"final bytes", content_type="video/mp4")
    data, mime = await fetch_media(f"{media_server.base_url}/start", follow_redirects=True, max_bytes=1024)
    assert data == b"final bytes"
    assert mime == "video/mp4"
    assert [r["path"] for r in media_server_2.records] == ["/final"]


@pytest.mark.parametrize("status", [302, 307])
async def test_fetch_media_unfollowed_redirect_is_a_fault(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer, media_server_2: _MediaServer, status: int
) -> None:
    """With ``follow_redirects`` off, a redirect is a permanent fault, not a silent empty body."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.redirect("/start", f"{media_server_2.base_url}/final", status=status)
    with pytest.raises(MediaFetchError) as ei:
        await fetch_media(f"{media_server.base_url}/start", max_bytes=1024)
    assert ei.value.status_code == status
    assert ei.value.transient is False
    assert media_server_2.records == []


async def test_fetch_media_drops_credential_on_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer, media_server_2: _MediaServer
) -> None:
    """A cross-origin (different-port) hop is sent with no Authorization; a same-origin hop keeps it."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    # Cross-origin: the redirect target is a different server (different port).
    media_server.redirect("/start", f"{media_server_2.base_url}/final")
    media_server_2.configure(body=b"cross", content_type="image/png")
    await fetch_media(
        f"{media_server.base_url}/start",
        headers={"Authorization": "Bearer SECRET"},
        follow_redirects=True,
        max_bytes=1024,
    )
    assert media_server.records[0]["authorization"] == "Bearer SECRET"  # first hop keeps it
    assert media_server_2.records[0]["authorization"] is None  # cross-origin hop dropped it

    # Same-origin control: a redirect within the same server keeps the credential.
    media_server.redirect("/again", f"{media_server.base_url}/local-final")
    media_server.configure(body=b"same", content_type="image/png")
    await fetch_media(
        f"{media_server.base_url}/again",
        headers={"Authorization": "Bearer SECRET"},
        follow_redirects=True,
        max_bytes=1024,
    )
    local_final = [r for r in media_server.records if r["path"] == "/local-final"]
    assert local_final[0]["authorization"] == "Bearer SECRET"


async def test_fetch_media_drops_auth_on_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer, media_server_2: _MediaServer
) -> None:
    """An ``auth=`` credential rides the same-origin hop's Authorization header but is dropped cross-origin."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    # Cross-origin: the redirect target is a different server (different port).
    media_server.redirect("/start", f"{media_server_2.base_url}/final")
    media_server_2.configure(body=b"cross", content_type="image/png")
    await fetch_media(
        f"{media_server.base_url}/start",
        auth=("user", "pass"),
        follow_redirects=True,
        max_bytes=1024,
    )
    # base64("user:pass") == dXNlcjpwYXNz
    assert media_server.records[0]["authorization"] == "Basic dXNlcjpwYXNz"  # first hop keeps it
    assert media_server_2.records[0]["authorization"] is None  # cross-origin hop dropped it

    # Same-origin control: a redirect within the same server keeps the derived credential.
    media_server.redirect("/again", f"{media_server.base_url}/local-final")
    media_server.configure(body=b"same", content_type="image/png")
    await fetch_media(
        f"{media_server.base_url}/again",
        auth=("user", "pass"),
        follow_redirects=True,
        max_bytes=1024,
    )
    local_final = [r for r in media_server.records if r["path"] == "/local-final"]
    assert local_final[0]["authorization"] == "Basic dXNlcjpwYXNz"


async def test_fetch_media_drops_all_caller_headers_on_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer, media_server_2: _MediaServer
) -> None:
    """A non-``Authorization`` caller header rides a same-origin hop but not a cross-origin one."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    # Cross-origin: the redirect target is a different server (different port).
    media_server.redirect("/start", f"{media_server_2.base_url}/final")
    media_server_2.configure(body=b"cross", content_type="image/png")
    await fetch_media(
        f"{media_server.base_url}/start",
        headers={"X-Vendor-Token": "SECRET"},
        follow_redirects=True,
        max_bytes=1024,
    )
    assert media_server.records[0]["x-vendor-token"] == "SECRET"  # first hop keeps it
    assert media_server_2.records[0]["x-vendor-token"] is None  # cross-origin hop dropped it

    # Same-origin control: a redirect within the same server keeps the header.
    media_server.redirect("/again", f"{media_server.base_url}/local-final")
    media_server.configure(body=b"same", content_type="image/png")
    await fetch_media(
        f"{media_server.base_url}/again",
        headers={"X-Vendor-Token": "SECRET"},
        follow_redirects=True,
        max_bytes=1024,
    )
    local_final = [r for r in media_server.records if r["path"] == "/local-final"]
    assert local_final[0]["x-vendor-token"] == "SECRET"


async def test_fetch_media_over_redirect_raises_without_url(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer
) -> None:
    """A redirect loop past ``max_redirects`` raises the guard's error, carrying no URL."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"], max_redirects=2)
    media_server.redirect("/loop", f"{media_server.base_url}/loop")
    with pytest.raises(UrlGuardError, match="exceeded max_redirects=2") as ei:
        await fetch_media(f"{media_server.base_url}/loop", follow_redirects=True, max_bytes=1024)
    assert media_server.base_url not in str(ei.value)


async def test_fetch_media_error_text_never_contains_url_or_token(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer, installed_logging: None
) -> None:
    """A fault on a token-bearing URL leaks the token neither into the error chain nor the logs."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    token = "1234567:AAtoken-SECRET"
    media_server.configure(body=b"missing", status=404, content_type="text/plain")

    captured: list[logging.LogRecord] = []
    handler = _CaptureHandler(captured)
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.addHandler(handler)
    try:
        with pytest.raises(MediaFetchError) as ei:
            await fetch_media(f"{media_server.base_url}/file/bot{token}/x.jpg", max_bytes=1024)
    finally:
        httpx_logger.removeHandler(handler)

    exc = ei.value
    assert exc.status_code == 404
    chain_text = ""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain_text += f"{current!s}{current!r}"
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    assert token not in chain_text
    assert "/file/bot" not in chain_text
    assert "127.0.0.1" in chain_text  # the host alone is fine

    log_text = "\n".join(r.getMessage() for r in captured)
    assert "HTTP Request:" in log_text  # httpx did log the request line
    assert token not in log_text
    assert "/file/bot" not in log_text
    assert "404" in log_text


@pytest.mark.parametrize(
    ("status", "transient"),
    [(503, True), (408, True), (429, True), (404, False), (410, False)],
)
async def test_fetch_media_status_error_transience(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer, status: int, transient: bool
) -> None:
    """A 5xx, a 408 (Request Timeout) or a 429 (Too Many Requests, a vendor throttle) is transient
    (redeliver); every other 4xx is permanent (notify)."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.configure(body=b"boom", status=status, content_type="text/plain")
    with pytest.raises(MediaFetchError) as ei:
        await fetch_media(f"{media_server.base_url}/x", max_bytes=1024)
    assert ei.value.status_code == status
    assert ei.value.transient is transient


async def test_fetch_media_transport_fault_is_redacted_and_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport fault re-wraps as a transient ``MediaFetchError`` carrying no URL/token."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    # A closed loopback port: the pin validates 127.0.0.1, then the connect is refused.
    closed = http.server.HTTPServer(("127.0.0.1", 0), _MediaHandler)
    port = closed.server_address[1]
    closed.server_close()
    token = "1234567:AAtoken-SECRET"
    with pytest.raises(MediaFetchError) as ei:
        await fetch_media(f"http://127.0.0.1:{port}/file/bot{token}/x", max_bytes=1024)
    exc = ei.value
    assert exc.status_code is None
    assert exc.transient is True
    assert token not in str(exc)
    assert exc.__context__ is None  # the URL-bearing httpx exception is not chained on
    assert exc.__cause__ is None


async def test_fetch_media_ssrf_blocks_private_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL resolving to a private/loopback address is blocked by the pinning transport."""
    _enable(monkeypatch)  # loopback NOT opted in
    with pytest.raises(UrlGuardError):
        await fetch_media("http://10.0.0.1/obj", max_bytes=1024)


async def test_fetch_media_ssrf_blocks_a_cross_origin_redirect_to_private(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer
) -> None:
    """The pin re-runs per hop: a redirect into private space is blocked at connect."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.redirect("/start", "http://10.0.0.1/internal")
    with pytest.raises(UrlGuardError):
        await fetch_media(f"{media_server.base_url}/start", follow_redirects=True, max_bytes=1024)


async def test_fetch_media_recovers_a_displaced_guard_error_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard rejection displaced into an httpx error's context surfaces as itself, URL-free.

    httpcore can wrap the backend's SSRF rejection in a transport error whose text carries
    the request URL; ``fetch_media`` recovers the rejection from the chain and re-raises it,
    never the URL-bearing wrapper.
    """
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    guard = UrlGuardError("SSRF guard blocked host 'host': resolves to non-public address.")

    def _raise_wrapped(*args: Any, **kwargs: Any) -> Any:
        wrapper = httpx.ConnectError("connection failed for http://host/file/botSECRET/x")
        wrapper.__context__ = guard
        raise wrapper

    monkeypatch.setattr(httpx.AsyncClient, "stream", _raise_wrapped)
    with pytest.raises(UrlGuardError, match="non-public") as ei:
        await fetch_media("http://host/file/botSECRET/x", max_bytes=1024)
    assert "botSECRET" not in str(ei.value)


def test_media_fetch_error_with_no_status_or_cause_reads_unknown() -> None:
    """A ``MediaFetchError`` built with neither a status nor a cause names the host and is transient."""
    exc = MediaFetchError(host="api.example.com")
    assert "api.example.com" in str(exc)
    assert "unknown error" in str(exc)
    assert exc.transient is True


async def test_httpx_request_line_is_redacted_on_success(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer, installed_logging: None
) -> None:
    """The SUCCESS-path httpx request-line log carries the origin + status, never the token path."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.configure(body=b"ok", content_type="image/png")
    token = "1234567:AAtoken-SECRET"

    captured: list[logging.LogRecord] = []
    handler = _CaptureHandler(captured)
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.addHandler(handler)
    try:
        data, _ = await fetch_media(f"{media_server.base_url}/file/bot{token}/img.png", max_bytes=1024)
    finally:
        httpx_logger.removeHandler(handler)

    assert data == b"ok"
    log_text = "\n".join(r.getMessage() for r in captured)
    assert "HTTP Request:" in log_text
    assert media_server.base_url in log_text  # scheme://host:port kept
    assert token not in log_text
    assert "/file/bot" not in log_text
    assert "200" in log_text

    # A second setup_logging never stacks a second redactor on the httpx logger.
    setup_logging(LoggingSettings(log_level="INFO"))
    redactors = [f for f in httpx_logger.filters if isinstance(f, _HttpxRequestLineRedactor)]
    assert len(redactors) == 1


def test_media_symbol_first_leaves_fetch_url_callable():
    """Resolving a media symbol before ``fetch_url`` must not turn ``net.fetch_url`` into a module."""
    probe = (
        "from tai42_kit.net import open_media_stream\n"
        "from tai42_kit.net import fetch_url\n"
        "from tai42_kit.net import MediaStream, MediaFetchError, fetch_media\n"
        "import tai42_kit.net as net\n"
        "assert callable(fetch_url) and callable(net.fetch_url), type(net.fetch_url)\n"
        "assert callable(fetch_media) and callable(open_media_stream)\n"
        "assert isinstance(MediaStream, type) and isinstance(MediaFetchError, type)\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_media_type_strips_parameters_and_lowercases() -> None:
    """``_media_type`` returns the bare media type, parameters stripped and lower-cased; absent/empty → None."""
    assert _media_type(httpx.Response(200, headers=[("Content-Type", "image/PNG; charset=binary")])) == "image/png"
    assert _media_type(httpx.Response(200, headers=[("Content-Type", "application/pdf")])) == "application/pdf"
    assert _media_type(httpx.Response(200)) is None
    assert _media_type(httpx.Response(200, headers=[("Content-Type", "  ; x=1")])) is None


def test_content_length_parses_absent_and_malformed() -> None:
    """``_content_length`` parses a numeric header, returns None when absent, and faults (permanent) on garbage."""
    assert _content_length(httpx.Response(200, headers=[("Content-Length", "1234")]), "h") == 1234
    assert _content_length(httpx.Response(200), "h") is None
    with pytest.raises(MediaFetchError) as ei:
        _content_length(httpx.Response(200, headers=[("Content-Length", "not-a-number")]), "api.example.com")
    assert ei.value.transient is False
    assert ei.value.status_code is None
    assert ei.value.__cause__ is None
    assert ei.value.__context__ is None


async def test_open_media_stream_streams_chunks_in_order(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer
) -> None:
    """The body is delivered incrementally over ``chunks`` and reassembles, in order, to the sent bytes."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    body = bytes(i % 256 for i in range(512 * 1024))
    media_server.configure(body=body, content_type="video/mp4")
    async with open_media_stream(f"{media_server.base_url}/obj") as stream:
        assert isinstance(stream, MediaStream)
        assert stream.content_type == "video/mp4"
        assert stream.content_length == len(body)
        assert stream.host == "127.0.0.1"
        collected = [chunk async for chunk in stream.chunks]
    assert len(collected) >= 2  # a half-MiB body over loopback arrives in several reads
    assert b"".join(collected) == body


async def test_open_media_stream_parses_content_type_and_length(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer
) -> None:
    """The final response's Content-Type (params stripped, lower-cased) and Content-Length reach the stream."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.configure(body=b"data", content_type="IMAGE/PNG; charset=utf-8")
    async with open_media_stream(f"{media_server.base_url}/obj") as stream:
        assert stream.content_type == "image/png"
        assert stream.content_length == 4


async def test_open_media_stream_content_type_and_length_absent_are_none(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer
) -> None:
    """A response without Content-Type/Content-Length yields ``None`` for both, body read to close."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.configure(body=b"body-bytes", content_type=None, include_content_length=False)
    async with open_media_stream(f"{media_server.base_url}/obj") as stream:
        assert stream.content_type is None
        assert stream.content_length is None
        assert b"".join([chunk async for chunk in stream.chunks]) == b"body-bytes"


async def test_open_media_stream_status_error_transience(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer
) -> None:
    """A 5xx at open is a transient fault; a 4xx at open is permanent — both raised before any chunk."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.configure(body=b"boom", status=503, content_type="text/plain")
    with pytest.raises(MediaFetchError) as ei_5xx:
        async with open_media_stream(f"{media_server.base_url}/x"):
            pass
    assert ei_5xx.value.status_code == 503
    assert ei_5xx.value.transient is True

    media_server.configure(body=b"nope", status=404, content_type="text/plain")
    with pytest.raises(MediaFetchError) as ei_4xx:
        async with open_media_stream(f"{media_server.base_url}/x"):
            pass
    assert ei_4xx.value.status_code == 404
    assert ei_4xx.value.transient is False


async def test_open_media_stream_transport_fault_is_redacted_and_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection fault at open re-wraps as a transient ``MediaFetchError`` carrying no URL/token."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    closed = http.server.HTTPServer(("127.0.0.1", 0), _MediaHandler)
    port = closed.server_address[1]
    closed.server_close()
    token = "1234567:AAtoken-SECRET"
    with pytest.raises(MediaFetchError) as ei:
        async with open_media_stream(f"http://127.0.0.1:{port}/file/bot{token}/x"):
            pass
    exc = ei.value
    assert exc.status_code is None
    assert exc.transient is True
    assert token not in str(exc)
    assert exc.__context__ is None
    assert exc.__cause__ is None


@pytest.mark.parametrize("url", ["file:///etc/hostname", "data:text/plain;base64,SGk="])
async def test_open_media_stream_unsupported_scheme_is_permanent_and_url_free(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """An unsupported URL scheme faults permanently (not transient) with no URL in the chain.

    httpx raises ``UnsupportedProtocol`` (an ``httpx.TransportError`` subclass) for a scheme it
    cannot dispatch; a redelivery would carry the same scheme, so the fault must be permanent —
    else the vendor redelivers a malformed URL forever.
    """
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    with pytest.raises(MediaFetchError) as ei:
        async with open_media_stream(url):
            pass
    exc = ei.value
    assert exc.transient is False
    assert exc.status_code is None
    chain_text = ""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain_text += f"{current!s}{current!r}"
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    assert url not in chain_text
    assert "/etc/hostname" not in chain_text


async def test_open_media_stream_mid_body_disconnect_raises_from_iterator(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer
) -> None:
    """A body that ends before its declared length faults from ``chunks``, transient and URL-free."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    token = "1234567:AAtoken-SECRET"
    # Declares far more bytes than it writes, then the loopback (HTTP/1.0) closes the socket.
    media_server.configure(body=b"partial", content_type="image/png", content_length_override="100000")

    async def _drain(url: str) -> None:
        async with open_media_stream(url) as stream:
            async for _chunk in stream.chunks:
                pass

    with pytest.raises(MediaFetchError) as ei:
        await _drain(f"{media_server.base_url}/file/bot{token}/x.png")
    exc = ei.value
    assert exc.transient is True
    assert exc.status_code is None
    chain_text = ""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain_text += f"{current!s}{current!r}"
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    assert token not in chain_text
    assert "/file/bot" not in chain_text


async def test_open_media_stream_drops_credentials_on_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer, media_server_2: _MediaServer
) -> None:
    """On the stream path a cross-origin hop is sent with no header and no ``auth``; a same-origin hop keeps them."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    media_server.redirect("/start", f"{media_server_2.base_url}/final")
    media_server_2.configure(body=b"cross", content_type="image/png")
    async with open_media_stream(
        f"{media_server.base_url}/start",
        headers={"X-Vendor-Token": "SECRET"},
        auth=("user", "pass"),
        follow_redirects=True,
    ) as stream:
        assert b"".join([chunk async for chunk in stream.chunks]) == b"cross"
    # base64("user:pass") == dXNlcjpwYXNz
    assert media_server.records[0]["authorization"] == "Basic dXNlcjpwYXNz"  # first hop keeps auth
    assert media_server.records[0]["x-vendor-token"] == "SECRET"  # and the caller header
    assert media_server_2.records[0]["authorization"] is None  # cross-origin hop dropped auth
    assert media_server_2.records[0]["x-vendor-token"] is None  # and the caller header

    media_server.redirect("/again", f"{media_server.base_url}/local-final")
    media_server.configure(body=b"same", content_type="image/png")
    async with open_media_stream(
        f"{media_server.base_url}/again",
        headers={"X-Vendor-Token": "SECRET"},
        follow_redirects=True,
    ) as stream:
        async for _chunk in stream.chunks:
            pass
    local_final = [r for r in media_server.records if r["path"] == "/local-final"]
    assert local_final[0]["x-vendor-token"] == "SECRET"  # same-origin hop keeps it


async def test_open_media_stream_closes_the_response_on_early_exit(
    monkeypatch: pytest.MonkeyPatch, media_server: _MediaServer
) -> None:
    """Leaving the context after reading one chunk (body not drained) still closes the response."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    body = bytes(i % 256 for i in range(512 * 1024))
    media_server.configure(body=body, content_type="video/mp4")

    closed: list[httpx.Response] = []
    original_aclose = httpx.Response.aclose

    async def _spy_aclose(self: httpx.Response) -> None:
        closed.append(self)
        await original_aclose(self)

    monkeypatch.setattr(httpx.Response, "aclose", _spy_aclose)
    async with open_media_stream(f"{media_server.base_url}/obj") as stream:
        async for _chunk in stream.chunks:
            break  # leave the body undrained
    assert closed  # the exit stack closed the final response even though it was not read to completion


async def test_open_media_stream_ssrf_blocks_private_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL resolving to a private/loopback address is blocked by the pinning transport at open."""
    _enable(monkeypatch)  # loopback NOT opted in
    with pytest.raises(UrlGuardError):
        async with open_media_stream("http://10.0.0.1/obj"):
            pass
