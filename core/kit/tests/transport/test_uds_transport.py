"""UDS transport internals: the SafeAsyncClient shutdown-race guard, the
per-client socket factory, and the connect_session wiring for both wire
protocols. The mcp client streams + ClientSession are faked — no real socket is
opened.
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from tai42_kit.transport import base_uds_transport as base
from tai42_kit.transport.base_uds_transport import SafeAsyncClient
from tai42_kit.transport.http_uds_transport import HTTPUDSTransport
from tai42_kit.transport.sse_uds_transport import SSEUDSTransport


async def test_safe_async_client_swallows_invalid_state_error(monkeypatch):
    client = SafeAsyncClient(base_url="http://127.0.0.1")

    async def _boom(*a, **k):
        raise asyncio.exceptions.InvalidStateError("shutdown race")

    monkeypatch.setattr(httpx.AsyncClient, "__aexit__", _boom)
    # The benign UDS shutdown race is swallowed, not propagated.
    await client.__aexit__(None, None, None)


async def test_safe_async_client_propagates_other_errors(monkeypatch):
    client = SafeAsyncClient(base_url="http://127.0.0.1")

    async def _boom(*a, **k):
        raise RuntimeError("real failure")

    monkeypatch.setattr(httpx.AsyncClient, "__aexit__", _boom)
    with pytest.raises(RuntimeError, match="real failure"):
        await client.__aexit__(None, None, None)


def test_socket_factory_builds_safe_client_over_uds():
    t = HTTPUDSTransport(socket_path="/tmp/s.sock")
    client = t._socket_factory(headers={"X": "1"})
    assert isinstance(client, SafeAsyncClient)
    assert client.follow_redirects is True
    # Default timeout applied when none is passed.
    assert client.timeout.read == 60.0


def test_socket_factory_respects_explicit_timeout():
    t = HTTPUDSTransport(socket_path="/tmp/s.sock")
    client = t._socket_factory(timeout=httpx.Timeout(5.0))
    assert client.timeout.read == 5.0


async def test_run_session_wraps_streams_into_client_session(monkeypatch):
    captured = {}

    class _FakeSession:
        def __init__(self, read, write, **kwargs):
            captured["read"], captured["write"], captured["kwargs"] = read, write, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(base, "ClientSession", _FakeSession)

    @asynccontextmanager
    async def _client_cm():
        # streamable-http style: (read, write, extra)
        yield ("READ", "WRITE", "EXTRA")

    t = HTTPUDSTransport(socket_path="/tmp/s.sock")
    async with t._run_session(_client_cm(), {"client_info": "ci"}) as session:
        assert isinstance(session, _FakeSession)
    assert captured["read"] == "READ"
    assert captured["write"] == "WRITE"
    assert captured["kwargs"] == {"client_info": "ci"}


async def _drive_connect_session(monkeypatch, transport, client_factory_name):
    captured = {}

    @asynccontextmanager
    async def _fake_client(*args, **kwargs):
        captured["url"] = kwargs.get("url")
        captured["headers"] = kwargs.get("headers")
        captured["factory"] = kwargs.get("httpx_client_factory")
        yield ("R", "W")

    class _FakeSession:
        def __init__(self, read, write, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(base, "ClientSession", _FakeSession)
    monkeypatch.setattr(client_factory_name[0], client_factory_name[1], _fake_client)
    async with transport.connect_session() as session:
        assert isinstance(session, _FakeSession)
    return captured


async def test_http_connect_session_uses_streamable_client(monkeypatch):
    import tai42_kit.transport.http_uds_transport as http_mod

    captured = await _drive_connect_session(
        monkeypatch, HTTPUDSTransport(socket_path="/tmp/s.sock"), (http_mod, "streamablehttp_client")
    )
    assert captured["url"].endswith("/mcp")
    assert captured["headers"]["Cache-Control"] == "no-cache"
    assert callable(captured["factory"])


async def test_sse_connect_session_uses_sse_client(monkeypatch):
    import tai42_kit.transport.sse_uds_transport as sse_mod

    captured = await _drive_connect_session(
        monkeypatch, SSEUDSTransport(socket_path="/tmp/s.sock"), (sse_mod, "sse_client")
    )
    assert captured["url"].endswith("/sse")
    assert captured["headers"]["Cache-Control"] == "no-cache"
