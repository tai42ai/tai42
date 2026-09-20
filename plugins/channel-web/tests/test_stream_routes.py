"""The SSE stream door — the streaming response, the missing/unusable-identity and
store-off refusals, the concurrent-stream cap, and the abandoned-open no-slot-leak."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from starlette.responses import StreamingResponse

import tai42_channel_web.routes  # noqa: F401  (route registration side-effect)
from tai42_channel_web import stream as stream_module

from .conftest import (
    _STREAM,
    IDENTITY,
    SESSION_TOKEN,
    VISITOR_ID,
    FakeRedis,
    _body,
    _close_body,
    _handler,
    _stream_request,
)

pytestmark = pytest.mark.usefixtures("web_env")


async def test_stream_returns_streaming_response(web_env, stub_app, registered_session: FakeRedis):
    from tai42_channel_web.store.transcript import append_message

    await append_message(IDENTITY, VISITOR_ID, "out", "already said")
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))
    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["x-content-type-options"] == "nosniff"
    # Draining the body releases the slot the door acquired — and the stream is the
    # caller's OWN transcript, replayed before the backlog marker.
    frames = [str(frame) async for frame in resp.body_iterator]
    assert frames[0].startswith("event: chat.message\ndata: ")
    assert '"text": "already said"' in frames[0]
    assert frames[1] == "event: chat.backlog_done\ndata: {}\n\n"


async def test_stream_without_a_session_cookie_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _STREAM)(_stream_request())
    assert resp.status_code == 401
    assert _body(resp)["code"] == "session_missing"


async def test_stream_with_an_unregistered_token_is_401(web_env, stub_app, fake_redis: FakeRedis):
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))
    assert resp.status_code == 401


async def test_stream_without_an_identity_says_it_is_missing(web_env, stub_app, registered_session: FakeRedis):
    resp = await _handler(stub_app, _STREAM)(_stream_request(query="", token=SESSION_TOKEN))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "the 'identity' query parameter is required"


@pytest.mark.parametrize("query", ["identity=%20%20", "identity=site%3Aalpha", f"identity={'a' * 257}"])
async def test_stream_with_an_unusable_identity_says_it_is_unusable(
    web_env, stub_app, registered_session: FakeRedis, query: str
):
    # A supplied-but-unusable identity is not a missing one; telling a caller their
    # parameter is absent when they sent it sends them looking in the wrong place.
    resp = await _handler(stub_app, _STREAM)(_stream_request(query=query, token=SESSION_TOKEN))
    assert resp.status_code == 400
    assert "must be a non-blank" in _body(resp)["error"]


async def test_stream_unconfigured_store_501(no_web_env, stub_app):
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))
    assert resp.status_code == 501
    assert _body(resp)["code"] == "web_transcript_store_off"


async def test_stream_over_the_visitor_cap_is_503(web_env, stub_app, registered_session: FakeRedis, monkeypatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WEB_MAX_STREAMS_PER_VISITOR", "1")
    reset_all_settings()
    handler = _handler(stub_app, _STREAM)
    first = await handler(_stream_request(token=SESSION_TOKEN))
    assert isinstance(first, StreamingResponse)

    # The door only CHECKS: the slot is taken by the generator, so the first stream
    # holds one only once its body is running.
    assert [frame async for frame in first.body_iterator] == ["event: chat.backlog_done\ndata: {}\n\n"]
    second = await handler(_stream_request(token=SESSION_TOKEN))
    assert isinstance(second, StreamingResponse)
    frames = second.body_iterator.__aiter__()
    assert "backlog_done" in str(await frames.__anext__())

    refused = await handler(_stream_request(token=SESSION_TOKEN))

    assert refused.status_code == 503
    assert _body(refused)["error"] == "too many open chat streams for this session; close one and try again"
    assert refused.headers["cache-control"] == "no-store"
    # The refusal must not have consumed the slot the live stream still holds.
    await _close_body(second)


async def test_a_stream_the_client_abandons_at_the_response_start_leaks_no_slot(
    web_env, stub_app, registered_session: FakeRedis
):
    # The real repro: on ASGI spec_version < 2.4 Starlette races the body against a
    # disconnect listener, and a client gone by then cancels the group at the
    # ``http.response.start`` send — the body generator is never advanced, so its
    # ``finally`` never runs. A slot taken at the door would be pinned for the life of
    # the process, four aborted opens locking a visitor out for their whole session.
    resp = await _handler(stub_app, _STREAM)(_stream_request(token=SESSION_TOKEN))
    assert isinstance(resp, StreamingResponse)
    sent: list[Any] = []

    async def send(message: Any) -> None:
        # A real send is I/O; suspending here is what lets the cancellation the
        # disconnect listener raised reach the body before its first frame.
        await asyncio.sleep(0)
        sent.append(message)

    async def receive() -> Any:
        return {"type": "http.disconnect"}

    await resp({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "method": "GET"}, receive, send)

    assert sent == [], "the body never ran, so nothing was sent"
    assert stream_module._open_streams == {}
