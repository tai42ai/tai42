"""The per-request admission record a serving generation counts, and the ASGI wrapper that keeps it.

Every HTTP request a generation serves is admitted on it by :class:`EpochAdmissionApp` and
counted until it ends, so a retire drains exactly that generation's live work. The record
travels with the request (on its ASGI scope and in the context), so a long-lived stream can
exempt itself from the retire drain and the reload gate can record the request that drives
or waits on it (:func:`current_request_admission`).
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastmcp.server.dependencies import get_http_request

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from tai42_skeleton.app.epoch import Epoch


@dataclass(eq=False)
class RequestAdmission:
    """The per-request admission record the serving epoch counts.

    A MUTABLE object shared between the admission wrapper and the request's own
    coroutine tree (mutation is visible even when the request's work runs in a child
    task, unlike a ``ContextVar`` set that would not propagate back to the wrapper).
    The request's in-flight slot is released for good exactly once: early by a long-lived
    stream that exempts itself from the retire drain (``long_lived``) or by a request that
    drives a gated body, else by the wrapper when the request ends. While the request
    waits on the reload gate its slot is not counted (it cannot move until the gate is
    released); it is counted again when the wait ends. ``finished`` is set when the
    request ends.
    """

    epoch: Epoch
    slot_released: bool = False
    long_lived: bool = False
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    _counted: bool = True
    _waits: int = 0

    def release_slot(self) -> None:
        """Release this request's in-flight slot on its generation for good; a second call is a no-op."""
        if self.slot_released:
            return
        self.slot_released = True
        self._uncount()

    def begin_wait(self) -> None:
        """Record one wait of this request on the reload gate; the first leaves the in-flight count."""
        self._waits += 1
        if self._waits == 1:
            self.epoch.waiting.add(self)
            self._uncount()
        self.epoch.note_gate_wait()

    def end_wait(self) -> None:
        """Record that one wait on the reload gate ended; the last returns a live request to the in-flight count."""
        self._waits -= 1
        if self._waits > 0:
            return
        self.epoch.waiting.discard(self)
        if not self._counted and not self.slot_released and not self.finished.is_set():
            self._counted = True
            self.epoch.admit()

    def _uncount(self) -> None:
        if self._counted:
            self._counted = False
            self.epoch.release()


# The ASGI scope key the admission wrapper stores each request's record under, so the
# record is found from the request itself wherever its scope travels (a sub-app's copied
# scope, the request an MCP SDK attaches to every message it routes).
_ADMISSION_SCOPE_KEY = "tai42.admission"

# The admission record of the request in flight on THIS coroutine tree (``None`` off a served
# request). Read through :func:`current_request_admission`, never directly: a task that
# copied a request's context (an MCP session's server task copies its ``initialize``
# request's) carries a record that does not name the request it is serving now.
_admission: ContextVar[RequestAdmission | None] = ContextVar("admission", default=None)


def current_request_admission() -> RequestAdmission | None:
    """The admission record of the request this code runs on behalf of, or ``None``.

    The HTTP request the MCP SDK's request context names comes first (fastmcp's
    ``get_http_request``): inside a streamable-HTTP tool body that is the POST carrying the
    ``tools/call``, while the ``_admission`` ContextVar there still holds the record of the
    request that created the session. When that request has already ended — on the SSE
    transport the message POST is answered before the call runs, and the call answers on
    the session's stream — or when there is none (a plain route handler), the ContextVar's
    record: on SSE the session stream's, which the session's calls run under. A record whose
    request already ended resolves to ``None``: a task that inherited a finished request's
    context acts for no request.
    """
    state: RequestAdmission | None = None
    try:
        request = get_http_request()
    except RuntimeError:
        request = None
    if request is not None:
        state = request.scope.get(_ADMISSION_SCOPE_KEY)
    if state is None or state.finished.is_set():
        state = _admission.get()
    if state is None or state.finished.is_set():
        return None
    return state


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
        # record the request that drives or waits on it (``current_request_admission``). The
        # record is a shared object, so a slot released by the request's own work is seen here.
        state = RequestAdmission(epoch=self._epoch)
        scope[_ADMISSION_SCOPE_KEY] = state
        admission_token = _admission.set(state)
        try:
            await self._app(scope, receive, send)
        finally:
            _admission.reset(admission_token)
            self._epoch.driving.discard(state)
            state.release_slot()
            state.finished.set()


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
    streamable-HTTP standalone stream ends when the close terminates its session. The inbox
    stream (a plain Starlette route on its own redis connection) and the SSE session stream
    (the session runs inside it) are not severed by the close: they keep running on the
    retired generation until the client disconnects, so that generation (its ASGI stack and
    that stack's collaborators) stays reachable, uncounted, until then, and its certification
    runs once its serving surface is collected after the disconnect. An exempted stream reads
    its configuration through the cached settings accessors at each use, so it keeps no
    settings object of its own across a reload.

    Releases this request's in-flight slot NOW so ``_drain_in_flight`` no longer waits on it,
    and marks the record ``long_lived``; the record releases the slot exactly once, so the
    admission wrapper does not release it a SECOND time at request end. A no-op off a served
    request. Only a genuinely long-lived stream calls this; every real / short request stays
    counted, so a retire STILL drains in-flight work (an MCP tool call, a sync REST tool run)
    BEFORE the transports close.
    """
    state = current_request_admission()
    if state is not None:
        state.long_lived = True
        state.release_slot()
