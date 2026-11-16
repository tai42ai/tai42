r"""The chat page's SSE generator: transcript backlog, then a live tail of the transcript and the working frame.

Reimplements the interactions-stream backlog/XREAD/keepalive loop over the kit
``RedisClient`` (a channel plugin never imports the skeleton). The shape is
load-bearing and copied faithfully:

* the tail cursor is captured BEFORE the backlog read, and the backlog is read only
  UP TO it (no live entry is missed, and none written during a slow replay is
  emitted twice — once by the backlog and again by the tail);
* the backlog is read one COUNT-bounded page at a time under a POOLED connection
  that is released BEFORE any of that page's frames is yielded — a slow SSE client
  suspends the generator between frames and must never pin the shared pool, and a
  long transcript must never be materialized whole (peak memory is one page);
* the live tail runs on a dedicated FRESH connection with the socket read timeout
  stripped (the keepalive XREAD blocks legitimately), bounded by an outer
  ``asyncio.wait_for`` (block window + grace) so a black-holed Redis raises loudly
  rather than hanging;
* the tail reads the transcript and the pair's working stream in one XREAD; the
  working cursor starts at ``0-0`` after the backlog, so the one retained working
  frame (the current state) reaches a page that just connected, and the backlog
  itself never carries one. One read's entries are emitted in the order of their ids'
  millisecond time (both streams' ids come from the one server clock), a working frame
  ahead of a transcript entry of the same millisecond and each stream's own entries in
  their stream order (an id's sequence part counts within its own stream only, so it
  never compares across the two; the sort is stable);
* the keepalive is DEADLINE-driven off a monotonic loop clock; a frame reaching the
  caller restarts the countdown, an idle window past the deadline emits ``":
  keepalive\\n\\n"``.

That dedicated connection is why the door admits a bounded number of streams, per
visitor and per process. The door only CHECKS (``check_stream_admission``, so an
over-cap caller gets a loud 503 instead of SSE headers); the slot itself is taken and
released inside the generator, which is the only place that can guarantee it is given
back — see ``check_stream_admission``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

from starlette.requests import Request

from tai42_channel_web.settings import WebSettings
from tai42_channel_web.store import connection, transcript, working
from tai42_channel_web.store.connection import _as_str

logger = logging.getLogger(__name__)


class StreamLimitError(RuntimeError):
    """No SSE stream slot is available — for this visitor or for this process.

    Carries both halves of the refusal: the exception's own text is the operator's
    detail (which cap, and whose), ``visitor_message`` is what the anonymous caller
    is told — and each cap says a different thing, so a visitor with too many tabs
    open is never told the server is full.
    """

    def __init__(self, detail: str, visitor_message: str) -> None:
        """Carry the operator ``detail`` and the anonymous caller's ``visitor_message``."""
        super().__init__(detail)
        self.visitor_message = visitor_message


# Open streams by visitor id. Every entry is one live generator holding one dedicated
# Redis connection; a released slot is deleted so the map stays the size of the live
# stream set.
_open_streams: Counter[str] = Counter()

_VISITOR_FULL = "too many open chat streams for this session; close one and try again"
_PROCESS_FULL = "this server is carrying too many chat streams; try again shortly"


def check_stream_admission(visitor_id: str, settings: WebSettings) -> None:
    """Admit one more SSE stream, or raise ``StreamLimitError``.

    A CHECK ONLY — it counts nothing. The slot is taken inside ``stream_transcript``,
    because that generator's ``finally`` is the only thing that can be relied on to
    give it back: an async generator the caller never advances runs no ``finally`` at
    all, and a stream aborted between the door and its first frame (a disconnect
    while the response start is being sent) is exactly that case. A slot taken at the
    door would then be held for the life of the process, on an anonymous public door.

    Checking here is what keeps the 503 AHEAD of the SSE headers. The cost is that
    the counts can momentarily exceed a cap by the number of opens in flight between
    this check and their first frame — a bounded, self-clearing overshoot, never a
    count that stays over.
    """
    if sum(_open_streams.values()) >= settings.max_streams_total:
        raise StreamLimitError(
            f"this process already carries its cap of {settings.max_streams_total} concurrent web chat streams",
            _PROCESS_FULL,
        )
    if _open_streams[visitor_id] >= settings.max_streams_per_visitor:
        raise StreamLimitError(
            f"visitor {visitor_id} already holds its cap of {settings.max_streams_per_visitor} open web chat streams",
            _VISITOR_FULL,
        )


def _drop(counts: Counter[str], key: str) -> None:
    counts[key] -= 1
    if counts[key] <= 0:
        del counts[key]


@contextmanager
def _stream_slot(visitor_id: str) -> Iterator[None]:
    """Hold one stream's slot for exactly the lifetime of the block.

    Entered from inside the SSE generator's body, so a generator that is never
    advanced never takes a slot and has none to leak.
    """
    _open_streams[visitor_id] += 1
    try:
        yield
    finally:
        _drop(_open_streams, visitor_id)


def _now() -> float:
    """Return the monotonic loop clock the keepalive deadline reads.

    A module-level seam so a test can drive the deadline without real wall-clock waits.
    """
    return asyncio.get_running_loop().time()


def _stream_milliseconds(entry_id: str) -> int:
    # The server-clock time part of a stream entry id (``<milliseconds>-<sequence>``).
    return int(entry_id.partition("-")[0])


async def _read_tail(
    redis: Any, identity: str, address: str, cursors: tuple[str, str], block_ms: int
) -> tuple[tuple[str, str], list[str]]:
    """One live-tail XREAD past the ``(transcript, working)`` cursors.

    Returns the advanced cursors and the new entries as SSE frames ordered by their ids'
    millisecond time, a working frame ahead of a transcript entry of the same millisecond and
    each stream's entries kept in their stream order (the sort is stable). A malformed transcript
    entry becomes a placeholder frame; a malformed working entry is logged and emitted as
    nothing (the page's frame lifetime ends what it last showed).
    """
    transcript_key = transcript.transcript_key(identity, address)
    working_key = working.working_key(identity, address)
    transcript_cursor, working_cursor = cursors
    result = await redis.xread({transcript_key: transcript_cursor, working_key: working_cursor}, block=block_ms)
    ordered: list[tuple[int, int, str]] = []
    for stream_name, messages in result or ():
        is_working = _as_str(stream_name) == working_key
        for message_id, raw_fields in messages:
            entry_id = _as_str(message_id)
            fields = {_as_str(k): _as_str(v) for k, v in raw_fields.items()}
            if is_working:
                working_cursor = entry_id
                rendered = working.working_frame(entry_id, fields)
                if rendered is None:
                    logger.warning("skipping malformed web working entry %s", entry_id)
                    continue
                ordered.append((_stream_milliseconds(entry_id), 0, rendered))
            else:
                transcript_cursor = entry_id
                ordered.append((_stream_milliseconds(entry_id), 1, transcript.tail_frame(entry_id, fields)))
    ordered.sort(key=lambda entry: (entry[0], entry[1]))
    return (transcript_cursor, working_cursor), [rendered for _ms, _rank, rendered in ordered]


async def stream_transcript(request: Request, identity: str, address: str, settings: WebSettings) -> AsyncIterator[str]:
    """Yield the pair's transcript backlog, a ``chat.backlog_done`` marker, then a live tail.

    The tail carries new transcript entries and the pair's working frames.

    The stream's slot is taken HERE and released however the stream ends; the door
    only checked that one was available (``check_stream_admission``).
    """
    with _stream_slot(address):
        batch = settings.backlog_batch_entries
        async with connection.pooled_redis_ctx() as redis:
            cursor = await transcript.capture_cursor(redis, identity, address)
            start, frames = await transcript.read_backlog_batch(redis, identity, address, "-", cursor, batch)
        while True:
            for entry in frames:
                yield entry
            if start is None:
                break
            # The pooled connection is re-taken per page and released again before
            # the page is yielded: no yield may ever sit inside that ``async with``.
            # The captured cursor bounds every page, so an entry written during a
            # slow replay is left for the tail rather than emitted by both.
            async with connection.pooled_redis_ctx() as redis:
                start, frames = await transcript.read_backlog_batch(redis, identity, address, start, cursor, batch)
        yield transcript.frame("chat.backlog_done", {})

        # The tail blocks per iteration; a fresh dedicated connection keeps it off
        # the shared pool. The outer wait_for bounds a black-holed Redis.
        cursors = (cursor, "0-0")
        next_keepalive = _now() + settings.keepalive_seconds
        async with connection.tail_redis_ctx() as tail_conn:
            while True:
                if await request.is_disconnected():
                    break
                block_seconds = max(0.0, next_keepalive - _now())
                try:
                    cursors, frames = await asyncio.wait_for(
                        _read_tail(tail_conn, identity, address, cursors, max(1, int(block_seconds * 1000))),
                        timeout=block_seconds + settings.blocking_grace_seconds,
                    )
                except TimeoutError as exc:
                    raise RuntimeError(
                        "web transcript SSE tail: redis XREAD did not return within the keepalive window "
                        f"+ {settings.blocking_grace_seconds}s grace — connection presumed stalled"
                    ) from exc
                for entry in frames:
                    yield entry
                # A frame reaching the caller doubles as liveness and restarts the
                # countdown; an idle window past the deadline emits the keepalive.
                if frames:
                    next_keepalive = _now() + settings.keepalive_seconds
                elif _now() >= next_keepalive:
                    yield ": keepalive\n\n"
                    next_keepalive = _now() + settings.keepalive_seconds
