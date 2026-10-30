"""A call of a session served on a long-lived stream (SSE) is a request of its own to its generation.

The session runs inside its stream, which is exempt from the retire drain; each call runs in
its own task under that stream's record. The call is admitted on the stream's generation with
a record of its own, counted until its task has ended and the stream has written its reply (or
ended), and it is the request the reload gate records when the call drives or waits on it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from tai42_skeleton.app import admission as admission_mod
from tai42_skeleton.app import transport_stream
from tai42_skeleton.app.admission import (
    RequestAdmission,
    admit_session_call,
    current_request_admission,
    mark_current_request_drain_exempt,
    track_current_stream_replies,
)
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.app.reload_gate import gate_party
from tai42_skeleton.app.transport_stream import SessionCallAdmissionMiddleware

from .test_epoch import _served_request


def _reply_event(request_id: int) -> bytes:
    """The ``message`` event a session stream writes for the reply to ``request_id``."""
    reply = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {}})
    return f"event: message\r\ndata: {reply}\r\n\r\n".encode()


async def test_a_session_call_is_counted_until_its_task_ended_and_its_reply_is_written() -> None:
    epoch = Epoch(number=0)
    admitted = asyncio.Event()
    answer = asyncio.Event()
    seen: dict[str, Any] = {}

    async def _call() -> None:
        seen["call"] = admit_session_call(7)
        admitted.set()
        await answer.wait()

    async def _stream() -> None:
        mark_current_request_drain_exempt()
        replies = track_current_stream_replies()
        assert replies is not None
        call = asyncio.create_task(_call())
        await admitted.wait()
        seen["while running"] = epoch.in_flight
        answer.set()
        await call
        await asyncio.sleep(0)  # the task's done callbacks run
        seen["handed, not written"] = epoch.in_flight
        replies.wrote(_reply_event(7))
        seen["written"] = epoch.in_flight

    await _served_request(epoch, _stream)
    call = seen["call"]
    assert isinstance(call, RequestAdmission)
    assert call.session_call
    assert call.epoch is epoch
    assert call.finished.is_set()
    assert seen["while running"] == 1, "the session's call is not counted against the drain"
    assert seen["handed, not written"] == 1, "the call stopped counting before its reply was written"
    assert seen["written"] == 0


async def test_a_reply_written_before_the_task_ended_counts_the_call_until_the_task_ends() -> None:
    epoch = Epoch(number=0)
    admitted = asyncio.Event()
    answer = asyncio.Event()
    seen: dict[str, Any] = {}

    async def _call() -> None:
        admit_session_call("call-1")
        admitted.set()
        await answer.wait()

    async def _stream() -> None:
        mark_current_request_drain_exempt()
        replies = track_current_stream_replies()
        assert replies is not None
        call = asyncio.create_task(_call())
        await admitted.wait()
        reply = json.dumps({"jsonrpc": "2.0", "id": "call-1", "error": {"code": -1, "message": "cancelled"}})
        replies.wrote(f"data: {reply}\n\n".encode())
        seen["written, running"] = epoch.in_flight
        answer.set()
        await call
        await asyncio.sleep(0)
        seen["ended"] = epoch.in_flight

    await _served_request(epoch, _stream)
    assert seen == {"written, running": 1, "ended": 0}


async def test_the_end_of_the_stream_ends_a_call_whose_reply_was_never_written() -> None:
    """No reply can follow the stream's end, so nothing is left to wait for."""
    epoch = Epoch(number=0)
    seen: dict[str, Any] = {}

    async def _stream() -> None:
        mark_current_request_drain_exempt()
        replies = track_current_stream_replies()
        assert replies is not None
        await asyncio.create_task(_call_once())
        await asyncio.sleep(0)
        seen["unwritten"] = epoch.in_flight
        replies.end()
        seen["ended"] = epoch.in_flight

    async def _call_once() -> None:
        admit_session_call(1)

    await _served_request(epoch, _stream)
    assert seen == {"unwritten": 1, "ended": 0}


async def test_a_call_on_a_stream_that_does_not_track_its_replies_raises() -> None:
    epoch = Epoch(number=0)

    async def _call() -> None:
        admit_session_call(1)

    async def _stream() -> None:
        mark_current_request_drain_exempt()
        await asyncio.create_task(_call())

    with pytest.raises(RuntimeError, match="does not track the replies it writes"):
        await _served_request(epoch, _stream)


async def test_a_call_without_a_request_id_on_a_stream_raises() -> None:
    epoch = Epoch(number=0)

    async def _call() -> None:
        admit_session_call(None)

    async def _stream() -> None:
        mark_current_request_drain_exempt()
        track_current_stream_replies()
        await asyncio.create_task(_call())

    with pytest.raises(RuntimeError, match="without a JSON-RPC request id"):
        await _served_request(epoch, _stream)


async def test_the_call_resolves_its_own_record_even_while_the_post_that_delivered_it_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SSE message POST is answered before the call runs, and it can end a moment after the
    call started; the call acts for its own record, never for that POST."""
    epoch = Epoch(number=0)
    post = RequestAdmission(epoch=epoch)

    @dataclass
    class _Request:
        scope: dict[str, Any] = field(default_factory=lambda: {admission_mod._ADMISSION_SCOPE_KEY: post})

    seen: dict[str, Any] = {}

    async def _call() -> None:
        # Inside the call the MCP SDK's request context names the POST that delivered it.
        monkeypatch.setattr(admission_mod, "get_http_request", _Request)
        seen["call"] = admit_session_call(1)
        seen["resolved"] = current_request_admission()
        seen["party"] = gate_party().request

    async def _stream() -> None:
        mark_current_request_drain_exempt()
        replies = track_current_stream_replies()
        assert replies is not None
        await asyncio.create_task(_call())
        replies.end()

    await _served_request(epoch, _stream)
    assert isinstance(seen["call"], RequestAdmission)
    assert not post.finished.is_set()
    assert seen["resolved"] is seen["call"]
    assert seen["party"] is seen["call"]


async def test_code_under_a_stream_outside_any_call_acts_for_no_request() -> None:
    """A long-lived stream never finishes on its own, so nothing can wait for it: code that
    runs under it outside a call of its session records no party on the gate."""
    epoch = Epoch(number=0)
    seen: dict[str, Any] = {}

    async def _stream() -> None:
        mark_current_request_drain_exempt()
        seen["resolved"] = current_request_admission()
        seen["party"] = gate_party().request

    await _served_request(epoch, _stream)
    assert seen == {"resolved": None, "party": None}


async def test_no_call_is_admitted_outside_a_long_lived_stream() -> None:
    """A streamable-HTTP call is counted by its own POST; nothing more is admitted for it."""
    epoch = Epoch(number=0)
    seen: dict[str, Any] = {}

    async def _post() -> None:
        seen["call"] = admit_session_call(1)
        seen["in flight"] = epoch.in_flight

    await _served_request(epoch, _post)
    assert seen == {"call": None, "in flight": 1}
    assert admit_session_call(1) is None


@dataclass
class _RequestContext:
    request_id: object


@dataclass
class _FastMCPContext:
    request_context: _RequestContext | None


@dataclass
class _Context:
    method: str
    fastmcp_context: _FastMCPContext | None = field(
        default_factory=lambda: _FastMCPContext(request_context=_RequestContext(request_id=5))
    )


@pytest.mark.parametrize(("method", "admitted"), [("tools/call", [5]), ("tools/list", [5]), ("initialize", [])])
async def test_the_middleware_admits_every_request_but_initialize_under_its_request_id(
    monkeypatch: pytest.MonkeyPatch, method: str, admitted: list[object]
) -> None:
    """``initialize`` is answered inline by the session's receive loop before it reads its next
    message, so the session's end cannot overtake it; its task is that loop's, which lives as
    long as the session."""
    admissions: list[object] = []
    monkeypatch.setattr(transport_stream, "admit_session_call", admissions.append)

    async def _call_next(_context: Any) -> str:
        return "answered"

    result = await SessionCallAdmissionMiddleware().on_request(_Context(method), _call_next)  # type: ignore[arg-type]
    assert result == "answered"
    assert admissions == admitted


@pytest.mark.parametrize(
    "fastmcp_context", [None, _FastMCPContext(request_context=None)], ids=["no-context", "no-request-context"]
)
async def test_a_request_outside_an_mcp_request_context_is_admitted_without_an_id(
    monkeypatch: pytest.MonkeyPatch, fastmcp_context: _FastMCPContext | None
) -> None:
    """Without an MCP request context the middleware still asks for the call's admission, with no request id."""
    admissions: list[object] = []
    monkeypatch.setattr(transport_stream, "admit_session_call", admissions.append)

    async def _call_next(_context: Any) -> str:
        return "answered"

    context = _Context("tools/call", fastmcp_context=fastmcp_context)
    await SessionCallAdmissionMiddleware().on_request(context, _call_next)  # type: ignore[arg-type]
    assert admissions == [None]
