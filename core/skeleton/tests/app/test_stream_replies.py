"""A session stream settles each call once the bytes it wrote carry the call's JSON-RPC response."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from tai42_skeleton.app.stream_replies import StreamReplies


def _event(message: dict[str, Any], *, line_end: str = "\r\n", event: str | None = "message") -> bytes:
    lines = ([f"event: {event}"] if event is not None else []) + [f"data: {json.dumps(message, ensure_ascii=False)}"]
    return (line_end.join(lines) + line_end + line_end).encode()


def _response(request_id: object, **fields: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, **(fields or {"result": {}})}


def _expect(replies: StreamReplies, request_id: object) -> list[object]:
    settled: list[object] = []
    replies.expect(request_id, lambda: settled.append(request_id))
    return settled


@pytest.mark.parametrize("line_end", ["\r\n", "\n", "\r"], ids=["crlf", "lf", "cr"])
def test_a_written_response_settles_the_call_it_answers(line_end: str) -> None:
    replies = StreamReplies()
    settled = _expect(replies, 3)
    other = _expect(replies, 4)
    replies.wrote(_event(_response(3), line_end=line_end))
    assert (settled, other) == ([3], [])


def test_an_error_response_settles_its_call() -> None:
    replies = StreamReplies()
    settled = _expect(replies, "a")
    replies.wrote(_event(_response("a", error={"code": 0, "message": "Request cancelled"})))
    assert settled == ["a"]


def test_an_event_without_a_type_is_a_message() -> None:
    replies = StreamReplies()
    settled = _expect(replies, 1)
    replies.wrote(_event(_response(1), event=None))
    assert settled == [1]


def test_a_response_split_across_chunks_settles_only_once_its_last_chunk_is_written() -> None:
    """A split falls inside the JSON, inside a multi-byte character and between CR and LF."""
    replies = StreamReplies()
    settled = _expect(replies, 2)
    body = _event(_response(2, result={"text": "é" * 3}))
    cuts = [5, body.index("é".encode()) + 1, len(body) - 3, len(body) - 2]
    start = 0
    for cut in cuts:
        replies.wrote(body[start:cut])
        start = cut
        assert settled == []
    replies.wrote(body[start:])
    assert settled == [2]


# A reply of many megabytes, written in the small chunks a stream sends under backpressure.
_LARGE_REPLY_CHARS = 16 * 1024 * 1024
_SMALL_CHUNK_BYTES = 4 * 1024
# Far above the linear cost of reading the reply (tens of milliseconds) and far below the
# cost of copying the line read so far on every chunk (many seconds at this size).
_LARGE_REPLY_READ_BOUND_S = 1.0


def test_a_large_response_written_in_many_small_chunks_settles_once_in_linear_time() -> None:
    replies = StreamReplies()
    settled = _expect(replies, 9)
    body = _event(_response(9, result={"text": "x" * _LARGE_REPLY_CHARS}))
    last_chunk = len(body) - _SMALL_CHUNK_BYTES
    start = time.perf_counter()
    for offset in range(0, last_chunk, _SMALL_CHUNK_BYTES):
        replies.wrote(body[offset : min(offset + _SMALL_CHUNK_BYTES, last_chunk)])
    assert settled == []
    replies.wrote(body[last_chunk:])
    took = time.perf_counter() - start
    assert settled == [9]
    assert took < _LARGE_REPLY_READ_BOUND_S, f"reading the reply took {took:.2f} s"


def test_several_events_in_one_chunk_settle_each_call() -> None:
    replies = StreamReplies()
    first, second = _expect(replies, 1), _expect(replies, 2)
    replies.wrote(_event(_response(2)) + b": keep-alive\r\n\r\n" + _event(_response(1)))
    assert (first, second) == ([1], [2])


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": 5, "method": "sampling/createMessage", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
    ],
    ids=["server-request", "notification"],
)
def test_a_message_the_server_sends_of_its_own_settles_nothing(message: dict[str, Any]) -> None:
    replies = StreamReplies()
    settled = _expect(replies, 5)
    replies.wrote(_event(message))
    assert settled == []


def test_an_event_of_another_type_settles_nothing() -> None:
    replies = StreamReplies()
    settled = _expect(replies, 1)
    replies.wrote(b"event: endpoint\r\ndata: /messages/?session_id=0\r\n\r\n")
    replies.wrote(_event(_response(1), event="other"))
    assert settled == []


def test_calls_expected_under_the_same_id_settle_one_per_response() -> None:
    replies = StreamReplies()
    settled: list[str] = []
    replies.expect(1, lambda: settled.append("first"))
    replies.expect(1, lambda: settled.append("second"))
    replies.wrote(_event(_response(1)))
    assert settled == ["first"]
    replies.wrote(_event(_response(1)))
    assert settled == ["first", "second"]


def test_the_end_settles_every_call_still_expected_and_any_expected_after() -> None:
    replies = StreamReplies()
    first, second = _expect(replies, 1), _expect(replies, 2)
    replies.end()
    late = _expect(replies, 3)
    assert (first, second, late) == ([1], [2], [3])


def test_a_message_event_that_is_not_json_raises() -> None:
    replies = StreamReplies()
    _expect(replies, 1)
    with pytest.raises(json.JSONDecodeError):
        replies.wrote(b"data: {not json\r\n\r\n")
