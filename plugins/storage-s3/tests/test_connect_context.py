"""The pooled S3 client keeps no context of the caller whose request opened a connection.

A keep-alive connection outlives the request that opened it, and the transport and timers
the HTTP session creates for it copy the context they are created in. Each case sends a
request from a caller whose context holds a marker object and checks the marker is
collected once the caller is gone while the client, and its keep-alive connection, stay
pooled. The server is an in-process keep-alive HTTP stub. Every case runs on asyncio and,
where it exists, uvloop.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import sys
import weakref
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

from tai42_storage_s3 import settings as settings_module
from tai42_storage_s3.client import S3Client

_caller: contextvars.ContextVar[object] = contextvars.ContextVar("caller")

_LOOP_FACTORIES: list[tuple[str, Callable[[], asyncio.AbstractEventLoop]]] = [("asyncio", asyncio.new_event_loop)]
if sys.platform != "win32":
    import uvloop

    _LOOP_FACTORIES.append(("uvloop", uvloop.new_event_loop))

_OBJECT_BODY = b"x" * 2048


class _Marker:
    """An object only the caller's context holds."""


@pytest.fixture(autouse=True)
def _plain_http_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Plain-HTTP, path-style settings with static credentials; the endpoint is set per case."""
    for name, value in {
        "STORAGE_S3_SECURE": "false",
        "STORAGE_S3_ACCESS_KEY": "ak",
        "STORAGE_S3_SECRET_KEY": "sk",
        "STORAGE_S3_ADDRESSING_STYLE": "path",
    }.items():
        monkeypatch.setenv(name, value)
    settings_module.s3_settings.cache_clear()
    yield
    settings_module.s3_settings.cache_clear()


async def _marker_alive_after(call: Callable[[Any], Awaitable[None]], monkeypatch: pytest.MonkeyPatch) -> bool:
    """Run ``call`` with the pooled client from a caller whose context holds a marker.

    Returns whether the marker is still alive while the client stays pooled. The stub
    answers a ``GET`` with a fixed object body and anything else with an empty ``200``,
    keeping every connection open.
    """

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = await reader.read(4096)
                    if not chunk:
                        return
                    head += chunk
                body = _OBJECT_BODY if head.startswith(b"GET") else b""
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: keep-alive\r\n\r\n" % len(body) + body
                )
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    monkeypatch.setenv("STORAGE_S3_ENDPOINT", f"127.0.0.1:{server.sockets[0].getsockname()[1]}")
    settings_module.s3_settings.cache_clear()
    client = S3Client()
    try:

        async def caller() -> weakref.ref[_Marker]:
            marker = _Marker()
            _caller.set(marker)
            async with client.current() as s3:
                await call(s3)
            return weakref.ref(marker)

        marker = await asyncio.create_task(caller())
        # Let the HTTP session's keep-alive timers run with the connection idle.
        await asyncio.sleep(0.3)
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()
        return marker() is not None
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


async def _head_bucket(s3: Any) -> None:
    await s3.head_bucket(Bucket="bucket")


async def _get_object_and_read_its_body(s3: Any) -> None:
    response = await s3.get_object(Bucket="bucket", Key="key")
    async with response["Body"] as body:
        assert await body.read() == _OBJECT_BODY


@pytest.mark.parametrize("loop_factory", [f for _, f in _LOOP_FACTORIES], ids=[n for n, _ in _LOOP_FACTORIES])
@pytest.mark.parametrize(
    "call", [_head_bucket, _get_object_and_read_its_body], ids=["head_bucket", "get_object_and_read_its_body"]
)
def test_a_pooled_connection_keeps_no_context_of_the_caller_that_opened_it(
    loop_factory: Callable[[], asyncio.AbstractEventLoop],
    call: Callable[[Any], Awaitable[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert asyncio.run(_marker_alive_after(call, monkeypatch), loop_factory=loop_factory) is False


async def test_a_request_is_sent_from_an_empty_context_and_cancelled_with_its_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiobotocore.httpsession import AIOHTTPSession

    from tai42_storage_s3.client import _EmptyContextHTTPSession

    seen_by_the_send: list[object] = []
    sending = asyncio.Event()
    send_cancelled = asyncio.Event()

    async def blocked_send(self: AIOHTTPSession, request: Any) -> Any:
        seen_by_the_send.append(_caller.get(None))
        sending.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            send_cancelled.set()
            raise

    monkeypatch.setattr(AIOHTTPSession, "send", blocked_send)

    async def caller() -> None:
        _caller.set(_Marker())
        await _EmptyContextHTTPSession().send(object())

    task = asyncio.create_task(caller())
    await sending.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen_by_the_send == [None]
    assert send_cancelled.is_set()
