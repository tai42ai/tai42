"""The interactions inbox live-tail SSE feed and its ``/api/interactions/stream`` door."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.access_control.user import request_identity
from tai42_skeleton.app.epoch import mark_current_request_drain_exempt
from tai42_skeleton.app.http import http_surface
from tai42_skeleton.app.route_registry import DeclaredRouteMetadata
from tai42_skeleton.interactions.settings import (
    INTERACTIONS_NOT_CONFIGURED_CODE,
    INTERACTIONS_NOT_CONFIGURED_MESSAGE,
    InteractionsSettings,
)
from tai42_skeleton.interactions.store import (
    ADD_EVENT,
    ANSWERED_EVENT,
    REMOVED_EVENT,
    InteractionStore,
    as_str,
)
from tai42_skeleton.operations.interactions import _add_data

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# This submodule's OWN package object (the one it was imported as part of), captured at
# import time from ``sys.modules`` — NOT ``from tai42_skeleton.routers import interactions``,
# whose parent-attribute read is stale mid-reload. The seam symbols
# (``client_ctx``/``interactions_settings``/``interactions_store_configured``/
# ``_KEEPALIVE_SECONDS``/``_now``) are read through it at call time.
_pkg = sys.modules["tai42_skeleton.routers.interactions"]

# The first SSE frame every stream flushes at connect, BEFORE the tail's first
# (blocking) XREAD. An SSE comment (leading ``:``) is a no-op to EventSource and to a
# fetch-reader SSE client, but it makes the FIRST body byte arrive at connect time — so a
# client whose Fetch resolves the response only on the first body byte (Firefox; Chromium
# resolves on the headers) treats the stream as connected in ~0.1s instead of waiting up to
# ``_KEEPALIVE_SECONDS`` for the first keepalive. Without it, `useInteractionsStream`'s
# connect-time base refetch — which fires when the stream fetch resolves — is deferred a
# full keepalive interval on Firefox, leaving the inbox's pending list up to 15s stale on
# every connect.
_CONNECT_FRAME = ": connected\n\n"


def _now() -> float:
    """The monotonic loop clock the keepalive deadline reads. A module-level seam so
    a test can drive the deadline without real wall-clock waits."""
    return asyncio.get_running_loop().time()


def _frame(event: str, data: dict, event_id: str) -> str:
    # ``json.dumps`` of the whole payload is what stops an attacker-supplied
    # answer (with newlines / ``data:`` sequences) from injecting extra frames.
    # Frame values are JSON-native by construction (they round-trip through the
    # contract models), so a serialization failure is a server bug and raises.
    # ``id`` is the Redis stream message-id: the browser echoes it back as the
    # ``Last-Event-ID`` header on reconnect so the route resumes AFTER it (SSE
    # resume). It is a bare ``<ms>-<seq>`` token by construction, so it cannot
    # break the frame framing.
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data)}\n\n"


def _parse_stream_id(value: str) -> tuple[int, int]:
    # A Redis stream id is ``<ms>-<seq>``; a bare ``<ms>`` implies seq 0. A value
    # that does not parse (a Last-Event-ID the client never got from us) raises,
    # which the caller turns into the logged tail fallback rather than a crash.
    ms, _, seq = value.partition("-")
    return (int(ms), int(seq) if seq else 0)


async def _resume_cursor(r: Redis, store: InteractionStore, last_event_id: str | None) -> str:
    """Resolve the events-stream cursor for a (re)connect.

    With a ``Last-Event-ID`` that is still inside the retained stream window, resume
    the tail AFTER it so every event during the disconnect gap (the ``answered``
    frame included) is replayed. When the id is absent, malformed, or already trimmed
    off the head, fall back to the tail END — the connect-time behavior the client's
    pending-base reseed backs up — and LOG the fallback so a lost-resume is never
    silent. Trimming only ever removes the OLDEST ids, so an id at or after the
    smallest retained id has every later event still present and is safe to resume.
    """
    if last_event_id:
        try:
            requested = _parse_stream_id(last_event_id)
        except ValueError:
            logger.warning("interactions SSE resume: malformed Last-Event-ID %r; falling back to tail", last_event_id)
        else:
            head = await r.xrange(store.events_key, count=1)
            if head and requested >= _parse_stream_id(as_str(head[0][0])):
                return last_event_id
            logger.info(
                "interactions SSE resume: Last-Event-ID %s trimmed from the stream; "
                "falling back to tail + pending-base reseed",
                last_event_id,
            )
    tail = await r.xrevrange(store.events_key, count=1)
    return as_str(tail[0][0]) if tail else "0-0"


async def _frame_for_event(
    fields: dict[str, str],
    store: InteractionStore,
    conn: Any,
    cursor: str,
    *,
    restricted: bool,
    restricted_id: str | None,
) -> str | None:
    """Map ONE stream entry to a rendered SSE frame string, or ``None`` when it is
    filtered or skipped, applying the audience filter.

    A RESTRICTED caller (owner claim present) sees ONLY interactions addressed to it:
    an ``add`` frame is filtered on the record's ``audience == <own id>``, and an
    ``answered``/``removed`` frame on the audience the store stamps into the event
    payload. A malformed entry (missing required field) or an entry whose state pruned
    between the event and this read yields ``None`` — one bad or gone event never tears
    down the tail. A ``reason`` tag rides a removed frame when the store set one
    (``"cancelled"`` for an operator per-interaction cancel), absent otherwise."""
    event_type = fields.get("type")
    interaction_id = fields.get("interaction_id")
    group_id = fields.get("group_id")
    if event_type is None or interaction_id is None or group_id is None:
        logger.debug("skipping malformed stream event %s: missing required field", cursor)
        return None
    if event_type == ADD_EVENT:
        state = await store.get_state(conn, interaction_id)
        # A state pruned/expired between the event and this read has nothing left to
        # show — the add frame is skipped, matching the pending-only filter.
        if state is None:
            return None
        if restricted and state.request.audience != restricted_id:
            return None
        return _frame(ADD_EVENT, _add_data(state.request), cursor)
    if event_type in (ANSWERED_EVENT, REMOVED_EVENT):
        # An absent audience field is an unaddressed question, which a restricted
        # caller never sees.
        if restricted and fields.get("audience") != restricted_id:
            return None
        terminal_frame: dict[str, str] = {"interaction_id": interaction_id, "group_id": group_id}
        reason = fields.get("reason")
        if reason is not None:
            terminal_frame["reason"] = reason
        return _frame(event_type, terminal_frame, cursor)
    return None


async def _stream_events(request: Request, store: InteractionStore, settings: InteractionsSettings, cursor: str):
    """Resolve the caller's isolation identity once, then run the NEVER-completing live
    tail. This is a TAIL-ONLY stream: the pending set is served by the paged
    ``GET /api/interactions`` door, so the stream carries no historical backlog and no
    end-of-backlog marker — only the live add/answered/removed tail. ``cursor`` is the
    events-stream tail the route handler captured BEFORE returning the response, so any
    add published after the client has the response headers has an id past it and is
    guaranteed delivered (the generator body runs only once the response iterates).
    """
    _user_id, restricted_id = request_identity()
    restricted = restricted_id is not None

    # Exempt this request from its serving generation's retire drain now: this is a
    # plain Starlette route on its own redis connection — a reload's ``aclose`` closes
    # only the FastMCP session-manager, so it does NOT sever this stream; the tail can
    # never drain, so a retire that waited on it would burn its whole budget and, on a
    # fleet sibling, stall fleet-reload convergence. The stream self-terminates on client
    # disconnect. Only this long-lived stream opts out; real requests stay counted and
    # are drained normally.
    mark_current_request_drain_exempt()

    # Flush a no-op comment immediately, before the tail's first (blocking) XREAD: this
    # is the first body byte, so a fetch-reader client resolves the stream at connect and
    # runs its connect-time base refetch now — not up to a keepalive interval later (the
    # Firefox inbox-staleness fix; see `_CONNECT_FRAME`).
    yield _CONNECT_FRAME

    # The tail blocks ~15s per iteration; a dedicated fresh connection keeps it
    # off the shared pool the answer door needs. The socket read timeout is
    # stripped on this connection only — the keepalive XREAD blocks legitimately
    # for the keepalive window, which a blanket 5s read timeout would kill; the
    # outer wait_for below bounds a black-holed redis instead.
    tail_redis = settings.redis.model_copy(update={"socket_timeout": None})
    # Keepalive cadence is governed by a monotonic loop-clock DEADLINE, not by
    # whether a given XREAD returned events. Any global-stream event (including one
    # filtered out for a restricted caller) makes XREAD return early; tying the
    # keepalive to that would let a restricted caller infer other identities'
    # activity timing from keepalive jitter. Instead XREAD blocks only until the
    # next deadline, the keepalive fires when the deadline passes, and the deadline
    # resets only on a frame actually yielded to THIS caller — so the cadence is
    # identical whether or not other identities are active.
    next_keepalive = _pkg._now() + _pkg._KEEPALIVE_SECONDS
    async with _pkg.client_ctx(RedisClient, tail_redis, fresh=True) as tail_conn:
        while True:
            if await request.is_disconnected():
                break
            block_seconds = max(0.0, next_keepalive - _pkg._now())
            try:
                result = await asyncio.wait_for(
                    tail_conn.xread({store.events_key: cursor}, block=max(1, int(block_seconds * 1000))),
                    timeout=block_seconds + settings.blocking_grace_seconds,
                )
            except TimeoutError as exc:
                raise RuntimeError(
                    "interactions SSE tail: redis XREAD did not return within the keepalive "
                    f"window + {settings.blocking_grace_seconds}s grace — connection presumed stalled"
                ) from exc
            yielded = False
            for _stream, messages in result or ():
                for message_id, data in messages:
                    cursor = as_str(message_id)
                    fields = {as_str(k): as_str(v) for k, v in data.items()}
                    frame = await _frame_for_event(
                        fields, store, tail_conn, cursor, restricted=restricted, restricted_id=restricted_id
                    )
                    if frame is not None:
                        yield frame
                        yielded = True
            # The keepalive is deadline-driven: it fires whenever the monotonic
            # deadline passes and no frame reached THIS caller this window — whether
            # XREAD returned nothing OR only events filtered out for a restricted
            # caller. A frame that DID reach the caller restarts the countdown (it
            # doubles as liveness); a window of only filtered events leaves the
            # deadline untouched, so the caller's cadence stays independent of other
            # identities' volume and the connection never goes silent.
            if yielded:
                next_keepalive = _pkg._now() + _pkg._KEEPALIVE_SECONDS
            elif _pkg._now() >= next_keepalive:
                yield ": keepalive\n\n"
                next_keepalive = _pkg._now() + _pkg._KEEPALIVE_SECONDS


@http_surface().custom_route(
    "/api/interactions/stream",
    methods=["GET"],
    summary="Stream the interactions inbox live tail",
    tags=["interactions"],
    response_model=None,
    no_body_reason="Interactions inbox live tail: SSE StreamingResponse",
    declared=DeclaredRouteMetadata(
        reload_gated=False,
        reads_body=False,
        error_statuses=(401, 501),
        success_status=200,
    ),
    action="read",
)
async def stream(request: Request) -> Response:
    # OFF gate — BEFORE the StreamingResponse is constructed: an unconfigured store
    # answers a plain 501+code up front rather than sending 200 + SSE headers and
    # then dying mid-body when the generator reaches for an absent Redis.
    if not _pkg.interactions_store_configured():
        return JSONResponse(
            {"error": INTERACTIONS_NOT_CONFIGURED_MESSAGE, "code": INTERACTIONS_NOT_CONFIGURED_CODE},
            status_code=501,
        )
    settings = _pkg.interactions_settings()
    store = InteractionStore(settings.key_prefix)
    # SSE resume: a reconnecting client echoes the last-delivered stream id back as the
    # ``Last-Event-ID`` header (a ``?last_event_id=`` query param is the fallback for
    # transports that cannot set the header). With it the cursor resumes AFTER the gap;
    # without it (or when it has been trimmed) the tail END is captured instead.
    last_event_id = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    # Resolve the cursor on the pooled connection BEFORE the response is returned, so a
    # client that has received the response headers is guaranteed every later add: any
    # event published after this read has an id past the cursor. The connection is
    # released here — the never-completing tail below runs on its own dedicated
    # connection and must never pin the shared pool.
    async with _pkg.client_ctx(RedisClient, settings.redis) as r:
        cursor = await _resume_cursor(r, store, last_event_id)
    return StreamingResponse(
        _stream_events(request, store, settings, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
