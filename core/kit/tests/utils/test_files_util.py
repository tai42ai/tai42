"""files_util.url_to_filelike: download a URL into a named in-memory file.

The default (no injected client) path fetches through
:func:`tai42_kit.net.fetch_url`, so it inherits the SSRF guard at full strength.
The injected-client path defaults to a transport-independent pre-flight
(``guard=True``): the URL host is resolved and validated before the download,
and the streamed body is size-capped per chunk; ``guard=False`` is the explicit
caller-owns-policy bypass. Injected-client tests fake the httpx client — no
real network request is made (guarded tests target literal loopback addresses,
which resolve locally without DNS).
"""

from collections.abc import AsyncIterator, Callable
from io import BytesIO
from typing import cast

import pytest
from httpx import AsyncClient, HTTPStatusError, Request, Response

from tai42_kit.net import url_guard
from tai42_kit.net.url_guard import UrlGuardError, UrlGuardSettings
from tai42_kit.utils.runtime import files_util
from tai42_kit.utils.runtime.files_util import url_to_filelike


class _FakeResponse:
    def __init__(self, *, content: bytes = b"data", content_type: str | None = "image/png", status: int = 200) -> None:
        self.content = content
        self.headers = {"Content-Type": content_type} if content_type else {}
        self.status_code = status
        self.reason_phrase = "OK" if status == 200 else "Error"
        self.text = "body-text"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPStatusError(
                "bad status",
                request=Request("GET", "http://x/file"),
                response=Response(self.status_code),
            )


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def get(self, url: str) -> _FakeResponse:
        return self._response


class _FakeStreamResponse:
    """Stands in for a streamed httpx response: status, headers, chunk iteration."""

    def __init__(self, *, chunks: list[bytes], content_type: str | None = "image/png", status: int = 200) -> None:
        self._chunks = chunks
        self.headers = {"Content-Type": content_type} if content_type else {}
        self.status_code = status
        self.reason_phrase = "OK" if status == 200 else "Error"
        self.chunks_yielded = 0

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPStatusError(
                "bad status",
                request=Request("GET", "http://127.0.0.1/file"),
                response=Response(self.status_code),
            )

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.chunks_yielded += 1
            yield chunk


class _FakeStreamClient:
    """Fakes ``client.stream(...)`` (the guarded download path) and records calls."""

    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response
        self.stream_calls: list[str] = []

    def stream(self, method: str, url: str):
        self.stream_calls.append(url)
        response = self._response

        class _Ctx:
            async def __aenter__(self) -> _FakeStreamResponse:
                return response

            async def __aexit__(self, *exc_info: object) -> None:
                return None

        return _Ctx()


def _as_client(fake: object) -> AsyncClient:
    # A real httpx.AsyncClient would issue a network request; url_to_filelike only
    # calls .get() (bypass path) or .stream() (guarded path), which the fakes
    # stand in for with matching signatures.
    return cast(AsyncClient, fake)


def _guard(
    *, enabled: bool = True, allow_cidrs: list[str] | None = None, max_response_bytes: int | None = None
) -> Callable[[], UrlGuardSettings]:
    """A ``url_guard_settings`` stand-in returning a fixed test policy."""
    if max_response_bytes is None:
        settings = UrlGuardSettings(enabled=enabled, allow_cidrs=allow_cidrs or [])
    else:
        settings = UrlGuardSettings(
            enabled=enabled, allow_cidrs=allow_cidrs or [], max_response_bytes=max_response_bytes
        )
    return lambda: settings


async def test_url_to_filelike_returns_named_buffer():
    client = _FakeClient(_FakeResponse(content=b"PNGDATA", content_type="image/png"))
    filename, buf, content_type = await url_to_filelike("http://x/file", client=_as_client(client), guard=False)
    assert content_type == "image/png"
    assert filename.endswith(".png")
    assert isinstance(buf, BytesIO)
    assert buf.read() == b"PNGDATA"


async def test_url_to_filelike_content_type_with_params_keeps_extension():
    # A Content-Type carrying parameters must still yield the right extension;
    # the params are stripped before guessing.
    client = _FakeClient(_FakeResponse(content=b"x", content_type="image/png; charset=binary"))
    filename, _buf, content_type = await url_to_filelike("http://x/file", client=_as_client(client), guard=False)
    assert content_type == "image/png; charset=binary"
    assert filename.endswith(".png")


async def test_url_to_filelike_no_content_type_no_extension():
    client = _FakeClient(_FakeResponse(content=b"x", content_type=None))
    filename, _buf, content_type = await url_to_filelike("http://x/file", client=_as_client(client), guard=False)
    assert content_type is None
    # No content-type -> no guessed extension on the generated filename.
    assert "." not in filename


async def test_url_to_filelike_raises_on_http_error():
    client = _FakeClient(_FakeResponse(status=404))
    with pytest.raises(HTTPStatusError):
        await url_to_filelike("http://x/file", client=_as_client(client), guard=False)


async def test_url_to_filelike_default_path_routes_through_fetch_url(monkeypatch: pytest.MonkeyPatch):
    # With no injected client the download goes through fetch_url; the returned
    # (bytes, mime) drives the same filename/extension/content-type contract.
    async def fake_fetch_url(url: str) -> tuple[bytes, str | None]:
        assert url == "http://x/file"
        return b"PNGDATA", "image/png"

    monkeypatch.setattr(files_util, "fetch_url", fake_fetch_url)
    filename, buf, content_type = await url_to_filelike("http://x/file")
    assert content_type == "image/png"
    assert filename.endswith(".png")
    assert buf.read() == b"PNGDATA"


async def test_url_to_filelike_default_path_blocks_internal_host(monkeypatch: pytest.MonkeyPatch):
    # The default path inherits the SSRF guard via fetch_url: an internal target
    # is refused, not downloaded.
    monkeypatch.setattr(url_guard, "url_guard_settings", _guard())
    with pytest.raises(UrlGuardError):
        await url_to_filelike("http://10.0.0.1/doc")


async def test_url_to_filelike_injected_client_preflight_blocks_internal_host(monkeypatch: pytest.MonkeyPatch):
    # guard=True (the default) on an injected client: the pre-flight resolves and
    # validates the URL host before any request — a literal internal address is
    # refused and the client is never used.
    monkeypatch.setattr(url_guard, "url_guard_settings", _guard())
    client = _FakeStreamClient(_FakeStreamResponse(chunks=[b"INTERNAL"]))
    with pytest.raises(UrlGuardError):
        await url_to_filelike("http://169.254.169.254/latest/meta-data", client=_as_client(client))
    assert client.stream_calls == []


async def test_url_to_filelike_injected_client_preflight_rejects_hostless_url(monkeypatch: pytest.MonkeyPatch):
    # A URL without a host cannot be validated; the guarded injected path refuses
    # it loudly instead of handing it to the client unchecked.
    monkeypatch.setattr(url_guard, "url_guard_settings", _guard())
    client = _FakeStreamClient(_FakeStreamResponse(chunks=[b"x"]))
    with pytest.raises(UrlGuardError, match="no host"):
        await url_to_filelike("not-a-url", client=_as_client(client))
    assert client.stream_calls == []


async def test_url_to_filelike_injected_client_guarded_streams_body(monkeypatch: pytest.MonkeyPatch):
    # A validated target (loopback opted in via allow_cidrs) streams through the
    # caller's client; the chunks are assembled into the returned buffer.
    monkeypatch.setattr(url_guard, "url_guard_settings", _guard(allow_cidrs=["127.0.0.0/8"]))
    client = _FakeStreamClient(_FakeStreamResponse(chunks=[b"PNG", b"DATA"], content_type="image/png"))
    filename, buf, content_type = await url_to_filelike("http://127.0.0.1/file", client=_as_client(client))
    assert client.stream_calls == ["http://127.0.0.1/file"]
    assert content_type == "image/png"
    assert filename.endswith(".png")
    assert buf.read() == b"PNGDATA"


async def test_url_to_filelike_injected_client_guarded_enforces_size_cap(monkeypatch: pytest.MonkeyPatch):
    # The guarded injected path caps the streamed body per chunk: the raise fires
    # mid-stream on the chunk that crosses the cap, never after buffering the
    # response in full — the chunk behind the over-cap one is never pulled.
    monkeypatch.setattr(url_guard, "url_guard_settings", _guard(allow_cidrs=["127.0.0.0/8"], max_response_bytes=5))
    response = _FakeStreamResponse(chunks=[b"123", b"456", b"never-pulled"])
    client = _FakeStreamClient(response)
    with pytest.raises(UrlGuardError, match="max_response_bytes"):
        await url_to_filelike("http://127.0.0.1/file", client=_as_client(client))
    assert response.chunks_yielded == 2


async def test_url_to_filelike_injected_client_guarded_raises_on_http_error(monkeypatch: pytest.MonkeyPatch):
    # An HTTP error status on the guarded streamed download raises; the error body
    # is only sampled for the log, never read in full — sampling stops at the
    # snippet bound and the remaining chunks are left unconsumed.
    monkeypatch.setattr(url_guard, "url_guard_settings", _guard(allow_cidrs=["127.0.0.0/8"]))
    response = _FakeStreamResponse(chunks=[b"e" * 1500, b"never-sampled"], status=404)
    client = _FakeStreamClient(response)
    with pytest.raises(HTTPStatusError):
        await url_to_filelike("http://127.0.0.1/file", client=_as_client(client))
    assert response.chunks_yielded == 1


async def test_url_to_filelike_injected_client_guard_false_bypasses(monkeypatch: pytest.MonkeyPatch):
    # guard=False is the explicit caller-owns-policy opt-out: even with the guard
    # enabled and an internal target, the download runs on the supplied client and
    # never touches the guard.
    monkeypatch.setattr(url_guard, "url_guard_settings", _guard())
    client = _FakeClient(_FakeResponse(content=b"INTERNAL", content_type="image/png"))
    filename, buf, content_type = await url_to_filelike("http://10.0.0.1/doc", client=_as_client(client), guard=False)
    assert content_type == "image/png"
    assert filename.endswith(".png")
    assert buf.read() == b"INTERNAL"


async def test_url_to_filelike_guard_false_without_client_raises():
    # guard=False exists to hand policy to an injected client; without one there
    # is no client to own it, and the default path is always guarded — the
    # incoherent ask raises instead of being silently ignored.
    with pytest.raises(ValueError, match="guard=False needs an injected client"):
        await url_to_filelike("http://x/file", guard=False)


async def test_url_to_filelike_injected_client_kill_switch_wins_over_guard_true(monkeypatch: pytest.MonkeyPatch):
    # TAI_URL_GUARD_ENABLED=false wins globally: with the guard disabled, guard=True
    # on an injected client validates nothing and downloads on the plain .get() path.
    monkeypatch.setattr(url_guard, "url_guard_settings", _guard(enabled=False))
    client = _FakeClient(_FakeResponse(content=b"INTERNAL", content_type="image/png"))
    filename, buf, content_type = await url_to_filelike("http://10.0.0.1/doc", client=_as_client(client))
    assert content_type == "image/png"
    assert filename.endswith(".png")
    assert buf.read() == b"INTERNAL"
