"""The browser-replay transcript stream: one Redis STREAM per conversation, its
write-order gate, the appenders, and the backlog/tail reads."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from tai42_channel_web.settings import web_settings
from tai42_channel_web.store.connection import _as_str, _mint_id, _now_iso, _redis

logger = logging.getLogger(__name__)

MESSAGE_EVENT = "chat.message"
QUESTION_EVENT = "chat.question"
ANSWERED_EVENT = "chat.answered"
MEDIA_EVENT = "chat.media"
FORM_EVENT = "chat.form"


def _transcript_key(identity: str, address: str) -> str:
    return f"channel:web:transcript:{identity}:{address}"


def frame(event: str, data: dict[str, Any]) -> str:
    """One SSE frame. ``json.dumps`` of the whole payload is what keeps a newline or
    ``data:`` sequence in a message body from injecting a second frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _decode_entry(fields: dict[str, str]) -> str | None:
    """Re-emit a stored transcript entry as its SSE frame, or ``None`` when the
    entry is malformed (missing ``event``/``data``) — one bad entry is skipped, never
    fatal to the whole tail. The stored ``data`` is already compact JSON."""
    event = fields.get("event")
    data = fields.get("data")
    if event is None or data is None:
        return None
    return f"event: {event}\ndata: {data}\n\n"


# One lock per ``(identity, address)`` conversation, refcounted so the map stays the
# size of the conversations currently writing. Per PROCESS, which is where the order
# is decided: a turn's reply is spawned by the same process that accepted the message.
_transcript_locks: dict[tuple[str, str], asyncio.Lock] = {}
_transcript_lock_users: Counter[tuple[str, str]] = Counter()


@asynccontextmanager
async def transcript_order(identity: str, address: str) -> AsyncIterator[None]:
    """Hold one conversation's transcript write order.

    The message door holds it across ``accept`` until the visitor's own frame is
    written; every agent-side append takes it first. Without the gate a reply
    ``accept`` spawns can XADD before the message that caused it, and the page then
    replays the answer above the question."""
    key = (identity, address)
    lock = _transcript_locks.get(key)
    if lock is None:
        lock = _transcript_locks[key] = asyncio.Lock()
    _transcript_lock_users[key] += 1
    try:
        async with lock:
            yield
    finally:
        _transcript_lock_users[key] -= 1
        if _transcript_lock_users[key] <= 0:
            del _transcript_lock_users[key]
            del _transcript_locks[key]


async def _append(identity: str, address: str, event: str, data: dict[str, Any]) -> None:
    """XADD one frame to the pair's transcript stream, trimming EXACTLY to the
    max-entries cap and refreshing the key TTL — one pipeline, so an append is a
    single round trip and the TTL cannot be left behind by a lost second command."""
    settings = web_settings()
    key = _transcript_key(identity, address)
    async with _redis() as redis:
        pipe = redis.pipeline()
        pipe.xadd(
            key,
            {"event": event, "data": json.dumps(data)},
            maxlen=settings.transcript_max_entries,
            approximate=False,
        )
        pipe.expire(key, settings.transcript_ttl_seconds)
        await pipe.execute()


async def append_message(
    identity: str,
    address: str,
    direction: str,
    text: str,
    entry_id: str | None = None,
    client_message_id: str | None = None,
) -> str:
    """Append one ``chat.message`` entry and return its id. ``entry_id`` is supplied
    for an inbound message (the accept-returned turn id, so the frame's id joins the
    transcript to the bridge record); an outbound notify mints its own.

    ``client_message_id`` echoes the retry key the visitor sent back onto their own
    frame, so the page can match the replayed message to the bubble it drew
    optimistically and retire the duplicate after a lost response. It is absent from
    the frame when the sender sent none — the key is the page's, not the server's,
    and an invented one would match nothing."""
    message_id = entry_id if entry_id is not None else _mint_id()
    data: dict[str, Any] = {"id": message_id, "direction": direction, "text": text, "ts": _now_iso()}
    if client_message_id is not None:
        data["client_message_id"] = client_message_id
    await _append(identity, address, MESSAGE_EVENT, data)
    return message_id


async def append_media(
    identity: str,
    address: str,
    text: str,
    media: list[dict[str, Any]] | None = None,
    options: list[dict[str, Any]] | None = None,
    sections: list[dict[str, Any]] | None = None,
    header: dict[str, Any] | None = None,
    footer: str | None = None,
    location: dict[str, Any] | None = None,
) -> str:
    """Append one ``chat.media`` agent entry (a rich card) and return its id.

    Every optional key is carried in the frame ONLY when present — there is no
    empty-value shape, so a reader tells "absent" from "empty" and never renders an
    empty control row:

    * ``media`` — the display items, each ``{"kind", "url", "caption"?, "filename"?}``
      (``filename`` on a ``document`` only); carried only when non-empty.
    * ``options`` — the flat tappable option list, each a serialized :data:`Option`
      (``{"kind": "reply", "text", "description"?, "id"?}`` or
      ``{"kind": "link", "label", "url"}``); carried only when present.
    * ``sections`` — the sectioned alternative, each ``{"title", "rows": [reply, ...]}``;
      carried only when present. ``options`` and ``sections`` are mutually exclusive by
      contract, so at most one is ever set.
    * ``header`` — a single display-media item shown above the body, the same shape as a
      ``media`` entry; carried only when present.
    * ``footer`` — the short trailing line under the card; carried only when present.
    * ``location`` — a shared geographic point ``{"latitude", "longitude", "name"?,
      "address"?}``, rendered as a map-pin element; carried only when present."""
    entry_id = _mint_id()
    data: dict[str, Any] = {"id": entry_id, "direction": "out", "text": text, "ts": _now_iso()}
    if media:
        data["media"] = media
    if options is not None:
        data["options"] = options
    if sections is not None:
        data["sections"] = sections
    if header is not None:
        data["header"] = header
    if footer is not None:
        data["footer"] = footer
    if location is not None:
        data["location"] = location
    await _append(identity, address, MEDIA_EVENT, data)
    return entry_id


async def append_question(
    identity: str,
    address: str,
    interaction_id: str,
    question: str,
    answer_format: str,
    options: list[str] | None,
    timeout_at: datetime,
    callback_url: str | None = None,
    schema: dict[str, Any] | None = None,
    media: list[dict[str, Any]] | None = None,
    form_data: dict[str, Any] | None = None,
    pages: list[dict[str, Any]] | None = None,
) -> str:
    """Append one ``chat.question`` entry (the UI renders the per-format widget) and
    return its id.

    ``callback_url`` is a bearer ticket for the interaction, so it is carried in the
    frame ONLY when the widget itself must open it (the ``external`` format);
    otherwise the key is absent and the ticket never leaves the server, where every
    other format answers by interaction id through this plugin's own door.

    ``schema`` is the ``form`` question's JSON answer schema — display-input material
    the page's form widget renders, never a secret — carried in the frame ONLY when
    present (non-None exactly for the ``form`` format); otherwise the key is absent.

    ``form_data`` is the ``form`` question's per-send enrichment — ``{"values", "options"}``,
    the known values shown filled in and the per-send choice lists (``{"value", "label"?}``
    each) that replace a property's choices for this send — carried under the frame key
    ``data`` ONLY when present. ``pages`` is the form's step layout — each ``{"title",
    "fields"}`` — carried ONLY when present (absent means one page). Both ride the ``form``
    format alone, display-input material the widget renders, never part of the answer.

    ``media`` is the question's display items — each ``{"kind", "url", "caption"?,
    "filename"?}`` (``filename`` on a ``document`` only), the SAME frame shape a
    ``chat.media`` card carries so the page renders them with the same media-card
    component — carried in the frame ONLY when non-empty; otherwise the key is absent.
    It is display-only, never part of the answer."""
    entry_id = _mint_id()
    data: dict[str, Any] = {
        "id": entry_id,
        "interaction_id": interaction_id,
        "question": question,
        "answer_format": answer_format,
        "options": options,
        "timeout_at": timeout_at.astimezone(UTC).isoformat(),
        "ts": _now_iso(),
    }
    if callback_url is not None:
        data["callback_url"] = callback_url
    if schema is not None:
        data["schema"] = schema
    if form_data is not None:
        data["data"] = form_data
    if pages is not None:
        data["pages"] = pages
    if media:
        data["media"] = media
    await _append(identity, address, QUESTION_EVENT, data)
    return entry_id


async def append_answered(identity: str, address: str, interaction_id: str, answer: Any) -> str:
    """Append one ``chat.answered`` entry (the UI settles the question's widget) and
    return its id."""
    entry_id = _mint_id()
    await _append(
        identity,
        address,
        ANSWERED_EVENT,
        {"id": entry_id, "interaction_id": interaction_id, "answer": answer, "ts": _now_iso()},
    )
    return entry_id


async def append_form(
    identity: str,
    address: str,
    text: str,
    schema: dict[str, Any],
    token: str,
    media: list[dict[str, Any]] | None = None,
    location: dict[str, Any] | None = None,
    form_data: dict[str, Any] | None = None,
    pages: list[dict[str, Any]] | None = None,
) -> str:
    """Append one ``chat.form`` agent entry (an ask-less form card) and return its id.

    ``text`` is the form's prompt, ``schema`` the JSON answer schema the page's form
    widget renders — display-input material, never a secret. ``token`` names the
    submission door for THIS card (``POST /forms/{token}``); it is a capability on
    the card's own conversation only — the door checks the record against the
    caller's session, so a token replayed from a foreign transcript resolves to
    nothing. ``media`` is the card's display items, the same ``{"kind", "url",
    "caption"?, "filename"?}`` shape every other card carries, present ONLY when
    non-empty. ``location`` is a shared geographic point (the same map-pin shape a
    media card carries) a form may ride alongside its fields, present ONLY when set.
    ``form_data`` is the card's per-send enrichment — ``{"values", "options"}``, the
    prefilled values shown filled in and the per-send choice lists — and ``pages`` its
    step layout, each the same frame shape a form question carries, present ONLY when set."""
    entry_id = _mint_id()
    data: dict[str, Any] = {"id": entry_id, "text": text, "schema": schema, "token": token, "ts": _now_iso()}
    if media:
        data["media"] = media
    if location is not None:
        data["location"] = location
    if form_data is not None:
        data["data"] = form_data
    if pages is not None:
        data["pages"] = pages
    await _append(identity, address, FORM_EVENT, data)
    return entry_id


async def capture_cursor(redis: Any, identity: str, address: str) -> str:
    """The transcript's newest entry id, captured BEFORE the backlog read so no live
    entry arriving during the backlog is missed. Empty stream → ``"0-0"`` (``"$"``
    would drop an entry written before the first XREAD)."""
    tail = await redis.xrevrange(_transcript_key(identity, address), count=1)
    return _as_str(tail[0][0]) if tail else "0-0"


def _next_id(entry_id: str) -> str:
    """The smallest stream id strictly after ``entry_id``. XRANGE bounds are
    inclusive, so this is where the next page starts."""
    milliseconds, _, sequence = entry_id.partition("-")
    return f"{milliseconds}-{int(sequence or 0) + 1}"


async def read_backlog_batch(
    redis: Any, identity: str, address: str, start: str, end: str, count: int
) -> tuple[str | None, list[str]]:
    """One COUNT-bounded page of the transcript as SSE frames, plus the id the next
    page starts at (``None`` once the last page is read).

    Paging is what keeps a replay's peak memory at one page rather than a whole
    transcript held live for the length of the stream. ``end`` is the cursor the tail
    will resume from: reading past it would emit every entry written during a slow
    replay twice, once here and once from the tail. A malformed entry is skipped
    (logged) — one bad entry is never fatal to the replay."""
    entries = await redis.xrange(_transcript_key(identity, address), min=start, max=end, count=count)
    frames: list[str] = []
    for entry_id, fields in entries:
        rendered = _decode_entry({_as_str(k): _as_str(v) for k, v in fields.items()})
        if rendered is None:
            logger.warning("skipping malformed web transcript backlog entry %s", _as_str(entry_id))
            continue
        frames.append(rendered)
    if len(entries) < count:
        return None, frames
    return _next_id(_as_str(entries[-1][0])), frames


async def read_tail(redis: Any, identity: str, address: str, cursor: str, block_ms: int) -> tuple[str, list[str]]:
    """One live-tail XREAD past ``cursor``; returns the advanced cursor and the new
    entries as SSE frames (a malformed entry skipped, logged)."""
    key = _transcript_key(identity, address)
    result = await redis.xread({key: cursor}, block=block_ms)
    frames: list[str] = []
    for _stream, messages in result or ():
        for message_id, fields in messages:
            cursor = _as_str(message_id)
            rendered = _decode_entry({_as_str(k): _as_str(v) for k, v in fields.items()})
            if rendered is None:
                logger.warning("skipping malformed web transcript tail entry %s", cursor)
                continue
            frames.append(rendered)
    return cursor, frames
