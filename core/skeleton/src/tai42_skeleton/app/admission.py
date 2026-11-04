"""The admission record of each unit of work a serving generation counts, and the doors that keep it.

Every HTTP request a generation serves is admitted on it by :class:`EpochAdmissionApp` and
counted until it ends, so a retire drains exactly that generation's live work. The record
travels with the request (on its ASGI scope and in the context), so a long-lived stream can
exempt itself from the retire drain and the reload gate can record the work that drives or
waits on it (:func:`current_admission`). A call of an MCP session served on a long-lived
stream (the SSE transport, where every call answers on the session's stream) is admitted as
work of its own (:func:`admit_session_call`), counted until its task has ended and the stream
has written its reply or ended (:func:`track_current_stream_replies`). A backend job running
on the serving loop is admitted as work of its own for its run (:func:`admitted_job`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from fastmcp.server.dependencies import get_http_request

from tai42_skeleton.app.stream_replies import StreamReplies

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from tai42_skeleton.app.epoch import Epoch

# The unit of the generation's work an admission record counts: a served HTTP request, a call
# of a session served on a long-lived stream, or a backend job running on the serving loop.
AdmissionKind = Literal["served_request", "session_call", "job"]


@dataclass(eq=False)
class Admission:
    """The admission record of one unit of the generation's work the serving epoch counts.

    ``kind`` names the unit: a served request, a call of a session on a long-lived stream, or a
    backend job running on the serving loop.

    A MUTABLE object shared between the door that admitted the work and the work's own
    coroutine tree (mutation is visible even when the work runs in a child task, unlike a
    ``ContextVar`` set that would not propagate back to the door). The work's in-flight slot is
    released for good exactly once: early by a long-lived stream that exempts itself from the
    retire drain (``long_lived``) or by work that drives a gated body, else by the door when the
    work ends. While the work waits on work that may drive its generation's retire (the reload
    gate, a lock acquired through :func:`~tai42_skeleton.app.reload_gate.hold_recorded`, or any
    other :func:`~tai42_skeleton.app.reload_gate.recorded_wait`) its slot is not counted (it
    cannot move until that work ends); it is counted again when the wait ends. ``finished`` is
    set when the work ends. Only a served request's record carries the stream fields:
    ``long_lived`` marks a served request whose long-lived stream is exempt from the drain
    (:func:`mark_current_request_drain_exempt`), and ``replies`` is set on the record of a
    long-lived stream that carries its session's replies (:func:`track_current_stream_replies`).
    """

    epoch: Epoch
    kind: AdmissionKind
    slot_released: bool = False
    long_lived: bool = False
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    _counted: bool = True
    _waits: int = 0
    replies: StreamReplies | None = None

    def finish(self) -> None:
        """Record that the work ended: it drives nothing any more and its slot is released."""
        self.epoch.driving.discard(self)
        self.release_slot()
        self.finished.set()

    def release_slot(self) -> None:
        """Release this work's in-flight slot on its generation for good; a second call is a no-op."""
        if self.slot_released:
            return
        self.slot_released = True
        self._uncount()

    def begin_wait(self) -> None:
        """Record one wait of this work on a recorded lock; the first leaves the in-flight count."""
        self._waits += 1
        if self._waits == 1:
            self.epoch.waiting.add(self)
            self._uncount()
        self.epoch.note_gate_wait()

    def end_wait(self) -> None:
        """Record that one wait on a recorded lock ended; the last returns live work to the in-flight count."""
        self._waits -= 1
        if self._waits > 0:
            return
        self.epoch.waiting.discard(self)
        if not self._counted and not self.slot_released and not self.finished.is_set():
            self._counted = True
            self.epoch.admit()
        self.epoch.note_wait_ended()

    def _uncount(self) -> None:
        if self._counted:
            self._counted = False
            self.epoch.release()


# The ASGI scope key the admission wrapper stores each request's record under, so the
# record is found from the request itself wherever its scope travels (a sub-app's copied
# scope, the request an MCP SDK attaches to every message it routes).
_ADMISSION_SCOPE_KEY = "tai42.admission"

# The admission record of the work in flight on THIS coroutine tree (``None`` off admitted
# work). Read through :func:`current_admission`, never directly: a task that copied a
# request's context (an MCP session's server task copies its ``initialize`` request's)
# carries a record that does not name the request it is serving now.
_admission: ContextVar[Admission | None] = ContextVar("admission", default=None)


def current_admission() -> Admission | None:
    """The admission record of the work this code runs on behalf of, or ``None``.

    A call of a session served on a long-lived stream runs under its own record
    (:func:`admit_session_call`), which comes first: on the SSE transport the message POST
    that delivered the call is answered before the call runs and is not the request the call
    answers on. Otherwise the HTTP request the MCP SDK's request context names comes first
    (fastmcp's ``get_http_request``): inside a streamable-HTTP tool body that is the POST
    carrying the ``tools/call``, while the ``_admission`` ContextVar there still holds the
    record of the request that created the session. When that request has already ended, or
    when there is none (a plain route handler, an admitted backend job), the ContextVar's
    record. A record whose work already ended resolves to ``None`` (a task that inherited a
    finished request's context acts for no work), and so does a long-lived stream's: it never
    finishes on its own, so nothing can wait for it.
    """
    state = _admission.get()
    if state is not None and state.kind == "session_call" and not state.finished.is_set():
        return state
    state = None
    try:
        request = get_http_request()
    except RuntimeError:
        request = None
    if request is not None:
        state = request.scope.get(_ADMISSION_SCOPE_KEY)
    if state is None or state.finished.is_set():
        state = _admission.get()
    if state is None or state.finished.is_set() or state.long_lived:
        return None
    return state


def admit_session_call(request_id: object | None) -> Admission | None:
    """Admit the MCP call running in the current task as work of its own when its session runs on a stream.

    On the SSE transport a session runs inside its ``GET`` stream, which is exempt from the
    retire drain, and every call of the session runs in its own task under that stream's
    record while its message ``POST`` has already been answered. Such a call is admitted on
    the stream's generation with a record of its own, set as the current task's record, and
    counted until both the task has ended and the stream has written the reply to
    ``request_id`` (the call's JSON-RPC request id) or ended — so a retire drains it and waits
    for it when it drives a gated body, before the generation's close ends the session, and a
    serve worker's stop ends the stream only after the reply is written. Returns the call's
    record, or ``None`` (nothing admitted) when the current code runs under no live long-lived
    stream: a streamable-HTTP call is counted by its own ``POST``. Raises when the stream does
    not track the replies it writes, or the call has no request id (``None``): its reply could
    not be told apart, so the call could not be counted until it is answered.
    """
    stream = _admission.get()
    if stream is None or not stream.long_lived or stream.finished.is_set():
        return None
    if stream.replies is None:
        raise RuntimeError("an MCP session call runs on a long-lived stream that does not track the replies it writes")
    if request_id is None:
        raise RuntimeError("an MCP session call on a long-lived stream runs without a JSON-RPC request id")
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("an MCP session call is admitted from inside its own task")
    stream.epoch.admit()
    call = Admission(epoch=stream.epoch, kind="session_call")
    _admission.set(call)
    # The call ends once its task has ended and its reply is written (or its stream ended).
    outstanding = 2

    def settle() -> None:
        nonlocal outstanding
        outstanding -= 1
        if outstanding == 0:
            call.finish()

    stream.replies.expect(request_id, settle)
    task.add_done_callback(lambda _task: settle())
    return call


def track_current_stream_replies() -> StreamReplies | None:
    """Track the replies the CURRENT long-lived stream writes for the calls of the session it carries.

    Called by a stream that has exempted itself from the retire drain
    (:func:`mark_current_request_drain_exempt`) and carries its session's replies (the SSE
    transport's session stream). Each call admitted under it (:func:`admit_session_call`)
    stays counted until the returned tracker has seen its reply written; the stream reports
    what it writes and its own end to the tracker. Returns ``None`` off a served request, where
    no call is admitted either.
    """
    state = _admission.get()
    if state is None or not state.long_lived or state.finished.is_set():
        return None
    state.replies = StreamReplies()
    return state.replies


@contextmanager
def admitted_job() -> Iterator[None]:
    """Admit the enclosed backend job's run on the live serving generation while it runs on the serving loop.

    The job is counted in flight until the scope ends, so a reload's retire drains it within the
    drain budget before it closes the generation's loop-bound resources; its record is the
    current admission, so a recorded wait or a reload the job drives is recorded as the job's.
    A job on any other event loop, or with no serving loop bound, records nothing: it is not
    the serving generation's work, and its own loop's end releases what it kept.
    """
    # The epoch and the app instance import this module as they load, so both are imported at call time.
    from tai42_skeleton.app.epoch import current_epoch
    from tai42_skeleton.app.instance import bound_serving_loop

    serving = bound_serving_loop()
    if serving is None or serving is not asyncio.get_running_loop():
        yield
        return
    epoch = current_epoch()
    epoch.admit()
    record = Admission(epoch=epoch, kind="job")
    token = _admission.set(record)
    try:
        yield
    finally:
        _admission.reset(token)
        record.finish()


class EpochAdmissionApp:
    """ASGI wrapper that counts every request against the epoch that served it.

    The swap re-points the dispatch slot to the NEW epoch's wrapper, so a request
    that entered under the old wrapper still finishes there — the count is exact per
    generation and the retire drains only that generation's admitted work.
    Non-HTTP scopes pass straight through uncounted.
    """

    def __init__(self, app: ASGIApp, epoch: Epoch) -> None:
        """Bind this wrapper to ``app`` and the ``epoch`` it counts requests against."""
        self._app = app
        self._epoch = epoch

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Count the HTTP request against its epoch for the life of the call; pass non-HTTP scopes through."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        self._epoch.admit()
        # Publish this request's admission record, on the scope and in the context, so a
        # long-lived stream can exempt itself from the retire drain and the reload gate can
        # record the request that drives or waits on it (``current_admission``). The record is
        # a shared object, so a slot released by the request's own work is seen here.
        state = Admission(epoch=self._epoch, kind="served_request")
        scope[_ADMISSION_SCOPE_KEY] = state
        admission_token = _admission.set(state)
        try:
            await self._app(scope, receive, send)
        finally:
            _admission.reset(admission_token)
            state.finish()


def mark_current_request_drain_exempt() -> None:
    """Exempt the CURRENT request from its generation's in-flight RETIRE DRAIN.

    Called for a request that has committed to a LONG-LIVED stream which cannot finish on its
    own: the interactions inbox SSE (``GET /api/interactions/stream``) the Studio shell holds
    open, and an MCP transport's server-to-client stream (the streamable-HTTP standalone GET,
    the SSE session GET; see :mod:`tai42_skeleton.app.transport_stream`). Such a stream can
    never "drain", so a retire that WAITED on it would burn the whole drain budget, report it
    as a straggler and, on a fleet sibling, delay the reload ack by the budget. The retire must
    not wait on it.

    Whether the retire's transport close ends the stream is the caller's own fact. The
    streamable-HTTP standalone stream ends when the close terminates its session. The SSE
    session stream is ended by its generation's close, after the session's calls (each
    admitted on its own, :func:`admit_session_call`) have answered within the drain budget.
    The inbox stream (a plain Starlette route on its own redis connection) is not severed by
    the close: it keeps running on the retired generation until the client disconnects, so
    that generation (its ASGI stack and that stack's collaborators) stays reachable,
    uncounted, until then, and its certification runs once its serving surface is collected
    after the disconnect. An exempted stream reads its configuration through the cached
    settings accessors at each use, so it keeps no settings object of its own across a
    reload.

    Releases this request's in-flight slot NOW so ``_drain_in_flight`` no longer waits on it,
    and marks the record ``long_lived``; the record releases the slot exactly once, so the
    admission wrapper does not release it a SECOND time at request end. A no-op when the current
    work is not a served request (none, a session call or a backend job): only a request's own
    stream is exempted. Only a genuinely long-lived stream calls this; every real / short request stays
    counted, so a retire STILL drains in-flight work (an MCP tool call, a sync REST tool run)
    BEFORE the transports close.
    """
    state = current_admission()
    if state is not None and state.kind == "served_request":
        state.long_lived = True
        state.release_slot()
