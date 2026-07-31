"""Tests for ``fetch_url``: the SSRF-pinned, redirect-safe, size-capped httpx
download that returns ``(bytes, mime)``.

The download is exercised end-to-end against a real loopback server (the pinning
transport connects to a validated address, so an httpx-level mock cannot reach
it). Guard policy is injected per test.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tai42_kit.net import fetch_url, url_guard
from tai42_kit.net.fetch_url import _PinningBackend, _unwrap_guard_error
from tai42_kit.net.url_guard import UrlGuardError, UrlGuardSettings


def _enable(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> UrlGuardSettings:
    settings = UrlGuardSettings(enabled=True, **kwargs)
    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: settings)
    return settings


def _disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: UrlGuardSettings(enabled=False))


async def test_fetch_url_returns_bytes_and_content_type_when_guard_disabled(
    monkeypatch: pytest.MonkeyPatch, local_server: Any
) -> None:
    """With the guard off a plain client is used; the body and the response
    Content-Type are still returned as the ``(bytes, mime)`` tuple."""
    _disable(monkeypatch)
    local_server.configure(body=b"plain body", content_type="application/pdf")
    data, mime = await fetch_url(f"{local_server.base_url}/doc.pdf")
    assert data == b"plain body"
    assert mime == "application/pdf"


async def test_fetch_url_pins_and_captures_mime_under_guard(monkeypatch: pytest.MonkeyPatch, local_server: Any) -> None:
    """With loopback opted in, the guard resolves and pins to 127.0.0.1, the
    download succeeds through the pinning transport, and the response
    Content-Type is captured as the returned mime."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    local_server.configure(body=b"hello from loopback", content_type="image/png")
    data, mime = await fetch_url(f"{local_server.base_url}/img")
    assert data == b"hello from loopback"
    assert mime == "image/png"
    assert local_server.requests == ["GET"]


async def test_fetch_url_blocks_internal_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    with pytest.raises(UrlGuardError):
        await fetch_url("http://10.0.0.1/doc")


async def test_fetch_url_guards_the_redirect_hop(monkeypatch: pytest.MonkeyPatch, local_server: Any) -> None:
    """The loopback hop is allowed; the redirect to internal space is blocked at
    connect (the pinning backend validates every new connection), so the internal
    target is never reached — only the first hop hit the server."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    local_server.configure(location="http://10.0.0.1/internal")
    with pytest.raises(UrlGuardError):
        await fetch_url(f"{local_server.base_url}/start")
    assert local_server.requests == ["GET"]


async def test_fetch_url_never_buffers_a_redirect_hop_body(
    monkeypatch: pytest.MonkeyPatch, local_server: Any, redirect_target: Any
) -> None:
    """A redirect body is closed unread, so it can never exhaust memory.

    httpx's own ``follow_redirects`` reads each intermediate body in full before
    chasing ``Location``, which the streaming size cap does not see. ``fetch_url``
    follows hops itself instead; nothing calls ``Response.aread``, and the hop's
    body — here eight times the cap — is never accumulated.
    """
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"], max_response_bytes=1024)
    local_server.configure(location=f"{redirect_target.base_url}/final", body=b"x" * 8192)
    redirect_target.configure(body=b"ok", content_type="text/plain")

    buffered: list[int] = []
    original_aread = httpx.Response.aread

    async def _spy(self: httpx.Response) -> bytes:
        content = await original_aread(self)
        buffered.append(len(content))
        return content

    monkeypatch.setattr(httpx.Response, "aread", _spy)

    content, mime = await fetch_url(f"{local_server.base_url}/start")
    assert content == b"ok"
    assert mime == "text/plain"
    assert buffered == []
    assert redirect_target.requests == ["GET"]


async def test_fetch_url_closes_the_unread_redirect_hop(
    monkeypatch: pytest.MonkeyPatch, local_server: Any, redirect_target: Any
) -> None:
    """The redirect hop's connection is released even though its body is never read.

    httpx closes a response for you once its body is streamed to exhaustion, so the
    terminal response would be released with or without an explicit close. A hop is
    deliberately left unread, so nothing releases it but ``fetch_url`` itself.
    """
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    local_server.configure(location=f"{redirect_target.base_url}/final", body=b"hop body")
    redirect_target.configure(body=b"ok", content_type="text/plain")

    closed: list[int] = []
    original_aclose = httpx.Response.aclose

    async def _spy(self: httpx.Response) -> None:
        closed.append(self.status_code)
        await original_aclose(self)

    monkeypatch.setattr(httpx.Response, "aclose", _spy)

    content, _ = await fetch_url(f"{local_server.base_url}/start")
    assert content == b"ok"
    assert 302 in closed


async def test_fetch_url_raises_when_redirect_chain_exceeds_max_redirects(
    monkeypatch: pytest.MonkeyPatch, local_server: Any
) -> None:
    """A redirect loop is refused loudly once ``max_redirects`` hops are spent.

    ``max_redirects`` is a guard bound like ``max_response_bytes``, so breaching it
    raises the guard's own error rather than an httpx one.
    """
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"], max_redirects=2)
    local_server.configure(location=f"{local_server.base_url}/loop")
    with pytest.raises(UrlGuardError, match="exceeded max_redirects=2"):
        await fetch_url(f"{local_server.base_url}/loop")
    assert local_server.requests == ["GET", "GET", "GET"]


async def test_fetch_url_enforces_size_cap(monkeypatch: pytest.MonkeyPatch, local_server: Any) -> None:
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"], max_response_bytes=4)
    local_server.configure(body=b"way too many bytes", content_type="text/plain")
    with pytest.raises(UrlGuardError, match="exceeds max_response_bytes"):
        await fetch_url(f"{local_server.base_url}/big")


async def test_fetch_url_surfaces_guard_error_when_pin_is_blocked(
    monkeypatch: pytest.MonkeyPatch, local_server: Any
) -> None:
    """A guard rejection raised inside the network backend reaches the caller as a
    loud ``UrlGuardError``, and the target is never contacted. The rejection is
    injected via a monkeypatched ``resolve_and_validate``."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])

    async def blocked_resolve(host: str) -> str:
        raise UrlGuardError(f"SSRF guard blocked host {host!r}: pinned to non-public address.")

    monkeypatch.setattr(url_guard, "resolve_and_validate", blocked_resolve)
    with pytest.raises(UrlGuardError, match="non-public address"):
        await fetch_url(f"{local_server.base_url}/doc")
    assert local_server.requests == []


async def test_fetch_url_reports_the_resolver_failure_that_provoked_the_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejection raised while handling a resolver error reports that error as its cause.

    The rejection is chained off the resolver error exactly as ``resolve_and_validate``
    chains it. Crossing the network backend drops that explicit cause, leaving the
    resolver error reachable only through the rejection's context — which is what
    ``fetch_url`` re-chains onto, so the caller sees it again.
    """
    _enable(monkeypatch)

    async def failing_resolve(host: str) -> str:
        try:
            raise OSError("Name or service not known")
        except OSError as exc:
            raise UrlGuardError(f"SSRF guard: cannot resolve host {host!r}.") from exc

    monkeypatch.setattr(url_guard, "resolve_and_validate", failing_resolve)

    with pytest.raises(UrlGuardError, match="cannot resolve host") as ei:
        await fetch_url("http://example.invalid/doc")
    assert isinstance(ei.value.__cause__, OSError)


async def test_fetch_url_recovers_a_guard_error_masked_by_a_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, local_server: Any
) -> None:
    """A failure while closing the response must not hide the guard's rejection.

    The response is closed as the over-cap rejection propagates; an exception raised
    there displaces the rejection, which would otherwise reach the caller as a bare
    teardown error. The rejection is recovered from that exception's chain.
    """
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"], max_response_bytes=4)
    local_server.configure(body=b"way too many bytes", content_type="text/plain")

    async def _failing_aclose(self: httpx.Response) -> None:
        raise RuntimeError("connection teardown failed")

    monkeypatch.setattr(httpx.Response, "aclose", _failing_aclose)

    with pytest.raises(UrlGuardError, match="exceeds max_response_bytes"):
        await fetch_url(f"{local_server.base_url}/big")


async def test_fetch_url_raises_on_http_error(monkeypatch: pytest.MonkeyPatch, local_server: Any) -> None:
    """A non-guard transport failure (an HTTP error status) is not swallowed by
    the unwrap path — it propagates as its own error."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    local_server.configure(body=b"nope", status=404, content_type="text/plain")
    with pytest.raises(httpx.HTTPStatusError, match="404"):
        await fetch_url(f"{local_server.base_url}/missing")


async def _exploding_aiter_bytes(self: httpx.Response, chunk_size: int | None = None) -> Any:
    raise AssertionError("response body iterated before raise_for_status")
    yield b""  # unreachable; makes this an async generator


async def test_fetch_url_raises_for_status_before_reading_body_under_guard(
    monkeypatch: pytest.MonkeyPatch, local_server: Any
) -> None:
    """On the guarded branch ``raise_for_status`` fires from the stream's headers
    before any body iteration: a 500 raises ``HTTPStatusError`` and the body is
    never consumed — a patched ``aiter_bytes`` that would explode on use is never
    reached."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"])
    monkeypatch.setattr(httpx.Response, "aiter_bytes", _exploding_aiter_bytes)
    local_server.configure(body=b"error body that must not be read", status=500, content_type="text/plain")
    with pytest.raises(httpx.HTTPStatusError, match="500"):
        await fetch_url(f"{local_server.base_url}/boom")


async def test_fetch_url_over_cap_error_body_raises_http_status_not_guard_error(
    monkeypatch: pytest.MonkeyPatch, local_server: Any
) -> None:
    """An error response whose body would exceed the size cap surfaces as
    ``HTTPStatusError`` — the status is raised before the body is buffered, so the
    cap is never hit and the failure is not misreported as ``UrlGuardError``."""
    _enable(monkeypatch, allow_cidrs=["127.0.0.0/8"], max_response_bytes=4)
    local_server.configure(body=b"error page way over the cap", status=500, content_type="text/plain")
    with pytest.raises(httpx.HTTPStatusError, match="500"):
        await fetch_url(f"{local_server.base_url}/boom")


async def test_fetch_url_raises_for_status_before_reading_body_when_guard_disabled(
    monkeypatch: pytest.MonkeyPatch, local_server: Any
) -> None:
    """On the guard-disabled branch ``raise_for_status`` also fires before body
    iteration: a 500 raises ``HTTPStatusError`` without the patched, exploding
    ``aiter_bytes`` ever being reached."""
    _disable(monkeypatch)
    monkeypatch.setattr(httpx.Response, "aiter_bytes", _exploding_aiter_bytes)
    local_server.configure(body=b"error body that must not be read", status=500, content_type="text/plain")
    with pytest.raises(httpx.HTTPStatusError, match="500"):
        await fetch_url(f"{local_server.base_url}/boom")


async def test_pinning_backend_delegates_unix_socket_and_sleep() -> None:
    """The pinning backend only intercepts TCP connects (where the SSRF pin
    lives); unix-socket connects and sleeps pass straight through to the wrapped
    backend."""
    calls: dict[str, Any] = {}

    class _FakeWrapped:
        async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> str:
            calls["unix"] = (path, timeout)
            return "unix-stream"

        async def sleep(self, seconds: float) -> None:
            calls["sleep"] = seconds

    backend = _PinningBackend(_FakeWrapped())  # type: ignore[arg-type]
    assert await backend.connect_unix_socket("/tmp/sock", timeout=1.5) == "unix-stream"
    await backend.sleep(0.25)
    assert calls == {"unix": ("/tmp/sock", 1.5), "sleep": 0.25}


def test_unwrap_guard_error_finds_the_rejection_down_either_leg() -> None:
    """The rejection is recovered whether it was chained explicitly or displaced.

    An exception that displaces the rejection carries it in ``__context__``; one that
    is raised ``from`` it carries it in ``__cause__``. Both legs are walked, and a
    self-referential chain terminates rather than spinning.
    """
    guard_error = UrlGuardError("SSRF guard: blocked.")

    assert _unwrap_guard_error(guard_error) is guard_error

    displaced = RuntimeError("teardown failed")
    displaced.__context__ = guard_error
    assert _unwrap_guard_error(displaced) is guard_error

    chained = RuntimeError("mapped by a transport")
    chained.__cause__ = guard_error
    assert _unwrap_guard_error(chained) is guard_error

    unrelated = RuntimeError("nothing to do with the guard")
    assert _unwrap_guard_error(unrelated) is None

    looped = RuntimeError("loop")
    looped.__context__ = looped
    assert _unwrap_guard_error(looped) is None
