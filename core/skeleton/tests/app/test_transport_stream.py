"""The transport-stream middleware exempts exactly the transport's server-to-client stream from the drain."""

from __future__ import annotations

from typing import Any

import pytest

from tai42_skeleton.app import transport_stream
from tai42_skeleton.app.transport_stream import TransportStreamDrainExemptMiddleware


@pytest.fixture
def exemptions(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """One entry per exemption the middleware asks for."""
    calls: list[None] = []
    monkeypatch.setattr(transport_stream, "mark_current_request_drain_exempt", lambda: calls.append(None))
    return calls


async def _pass_through(stream_path: str, scope: dict[str, Any]) -> None:
    seen: list[dict[str, Any]] = []

    async def _inner(inner_scope, receive, send) -> None:
        seen.append(inner_scope)

    middleware = TransportStreamDrainExemptMiddleware(_inner, stream_path=stream_path, carries_replies=False)
    await middleware(scope, None, None)  # type: ignore[arg-type]
    assert seen == [scope], "the middleware did not pass the request through"


@pytest.mark.parametrize(
    ("stream_path", "scope"),
    [
        ("/mcp", {"type": "http", "method": "GET", "path": "/mcp"}),
        ("/sse", {"type": "http", "method": "GET", "path": "/host/sse", "root_path": "/host"}),
        ("/", {"type": "http", "method": "GET", "path": "/", "root_path": "/app/slug"}),
        ("/app/slug/sse", {"type": "http", "method": "GET", "path": "/app/slug/sse", "root_path": ""}),
    ],
    ids=["main", "under-a-host-mount", "streamable-sub-app", "sse-sub-app"],
)
async def test_a_get_on_the_stream_path_is_exempted(
    stream_path: str, scope: dict[str, Any], exemptions: list[None]
) -> None:
    await _pass_through(stream_path, scope)
    assert len(exemptions) == 1


@pytest.mark.parametrize(
    "scope",
    [
        {"type": "http", "method": "POST", "path": "/mcp"},
        {"type": "http", "method": "DELETE", "path": "/mcp"},
        {"type": "http", "method": "GET", "path": "/mcp/other"},
        {"type": "http", "method": "GET", "path": "/api/health"},
        {"type": "lifespan"},
        {"type": "websocket", "path": "/mcp"},
    ],
    ids=["post", "delete", "get-below", "get-elsewhere", "lifespan", "websocket"],
)
async def test_every_other_request_stays_counted(scope: dict[str, Any], exemptions: list[None]) -> None:
    await _pass_through("/mcp", scope)
    assert exemptions == []


async def _messages_through(
    stream_path: str, scope: dict[str, Any], sent: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Send ``sent`` through the middleware from an inner app and return what reached the server."""
    received: list[dict[str, Any]] = []

    async def _inner(_scope, _receive, send) -> None:
        for message in sent:
            await send(message)

    async def _send(message: dict[str, Any]) -> None:
        received.append(message)

    middleware = TransportStreamDrainExemptMiddleware(_inner, stream_path=stream_path, carries_replies=False)
    await middleware(scope, None, _send)  # type: ignore[arg-type]
    return received


_COMPLETE_RESPONSE = [
    {"type": "http.response.start", "status": 200, "headers": []},
    {"type": "http.response.body", "body": b"event: endpoint\r\n\r\n", "more_body": True},
    {"type": "http.response.body", "body": b"", "more_body": False},
]
_PLACEHOLDER = [
    {"type": "http.response.start", "status": 200, "headers": []},
    {"type": "http.response.body", "body": b"", "more_body": False},
]


async def test_the_stream_requests_placeholder_after_its_completed_response_is_dropped(
    exemptions: list[None],
) -> None:
    """A server-ended SSE session completes its stream's response, then the transport's handler
    returns its placeholder response; only the completed response reaches the server."""
    scope = {"type": "http", "method": "GET", "path": "/sse"}
    assert await _messages_through("/sse", scope, _COMPLETE_RESPONSE + _PLACEHOLDER) == _COMPLETE_RESPONSE


async def test_any_other_requests_messages_all_reach_the_server(exemptions: list[None]) -> None:
    scope = {"type": "http", "method": "POST", "path": "/sse"}
    sent = _COMPLETE_RESPONSE + _PLACEHOLDER
    assert await _messages_through("/sse", scope, sent) == sent


class _RecordedReplies:
    """Records what a stream reports to its reply tracker, with what had reached the server by then."""

    def __init__(self, received: list[dict[str, Any]]) -> None:
        self.received = received
        self.log: list[tuple[str, bytes, int]] = []

    def wrote(self, body: bytes) -> None:
        self.log.append(("wrote", body, len(self.received)))

    def end(self) -> None:
        self.log.append(("end", b"", len(self.received)))


async def test_a_stream_carrying_replies_reports_each_chunk_once_sent_and_its_end(
    monkeypatch: pytest.MonkeyPatch, exemptions: list[None]
) -> None:
    received: list[dict[str, Any]] = []
    replies = _RecordedReplies(received)
    monkeypatch.setattr(transport_stream, "track_current_stream_replies", lambda: replies)

    async def _inner(_scope, _receive, send) -> None:
        for message in _COMPLETE_RESPONSE:
            await send(message)

    async def _send(message: dict[str, Any]) -> None:
        received.append(message)

    scope = {"type": "http", "method": "GET", "path": "/sse"}
    middleware = TransportStreamDrainExemptMiddleware(_inner, stream_path="/sse", carries_replies=True)
    await middleware(scope, None, _send)  # type: ignore[arg-type]
    assert replies.log == [("wrote", b"event: endpoint\r\n\r\n", 2), ("wrote", b"", 3), ("end", b"", 3)]


async def test_a_stream_carrying_replies_reports_its_end_when_its_app_raises(
    monkeypatch: pytest.MonkeyPatch, exemptions: list[None]
) -> None:
    replies = _RecordedReplies([])
    monkeypatch.setattr(transport_stream, "track_current_stream_replies", lambda: replies)

    async def _inner(_scope, _receive, _send) -> None:
        raise ConnectionError("the client went away")

    scope = {"type": "http", "method": "GET", "path": "/sse"}
    middleware = TransportStreamDrainExemptMiddleware(_inner, stream_path="/sse", carries_replies=True)
    with pytest.raises(ConnectionError):
        await middleware(scope, None, None)  # type: ignore[arg-type]
    assert replies.log == [("end", b"", 0)]


async def test_a_stream_not_carrying_replies_tracks_none(
    monkeypatch: pytest.MonkeyPatch, exemptions: list[None]
) -> None:
    tracked: list[None] = []
    monkeypatch.setattr(transport_stream, "track_current_stream_replies", lambda: tracked.append(None))
    await _pass_through("/mcp", {"type": "http", "method": "GET", "path": "/mcp"})
    assert tracked == []
