"""A call of a session served on a long-lived stream (SSE) is a request of its own to its generation.

The session runs inside its stream, which is exempt from the retire drain; each call runs in
its own task under that stream's record. The call is admitted on the stream's generation with
a record of its own, counted until its task ends (after the SDK handed its reply to the
transport), and it is the request the reload gate records when the call drives or waits on it.
"""

from __future__ import annotations

import asyncio
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
)
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.app.reload_gate import gate_party
from tai42_skeleton.app.transport_stream import SessionCallAdmissionMiddleware

from .test_epoch import _served_request


async def test_a_session_call_is_counted_from_its_admission_until_its_task_ends() -> None:
    epoch = Epoch(number=0)
    admitted = asyncio.Event()
    answer = asyncio.Event()
    seen: dict[str, Any] = {}

    async def _call() -> None:
        seen["call"] = admit_session_call()
        admitted.set()
        await answer.wait()

    async def _stream() -> None:
        mark_current_request_drain_exempt()
        call = asyncio.create_task(_call())
        await admitted.wait()
        seen["while running"] = epoch.in_flight
        answer.set()
        await call
        await asyncio.sleep(0)  # the task's done callbacks run
        seen["after"] = epoch.in_flight

    await _served_request(epoch, _stream)
    call = seen["call"]
    assert isinstance(call, RequestAdmission)
    assert call.session_call
    assert call.epoch is epoch
    assert call.finished.is_set()
    assert seen["while running"] == 1, "the session's call is not counted against the drain"
    assert seen["after"] == 0


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

    monkeypatch.setattr(admission_mod, "get_http_request", _Request)
    seen: dict[str, Any] = {}

    async def _call() -> None:
        seen["call"] = admit_session_call()
        seen["resolved"] = current_request_admission()
        seen["party"] = gate_party().request

    async def _stream() -> None:
        mark_current_request_drain_exempt()
        await asyncio.create_task(_call())

    await _served_request(epoch, _stream)
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
        seen["call"] = admit_session_call()
        seen["in flight"] = epoch.in_flight

    await _served_request(epoch, _post)
    assert seen == {"call": None, "in flight": 1}
    assert admit_session_call() is None


@dataclass
class _Context:
    method: str


@pytest.mark.parametrize(("method", "admitted"), [("tools/call", 1), ("tools/list", 1), ("initialize", 0)])
async def test_the_middleware_admits_every_request_but_initialize(
    monkeypatch: pytest.MonkeyPatch, method: str, admitted: int
) -> None:
    """``initialize`` is answered inline by the session's receive loop before it reads its next
    message, so the session's end cannot overtake it; its task is that loop's, which lives as
    long as the session."""
    admissions: list[None] = []
    monkeypatch.setattr(transport_stream, "admit_session_call", lambda: admissions.append(None))

    async def _call_next(_context: Any) -> str:
        return "answered"

    result = await SessionCallAdmissionMiddleware().on_request(_Context(method), _call_next)  # type: ignore[arg-type]
    assert result == "answered"
    assert len(admissions) == admitted
