"""Session registrations + transcript streams + pending-question records
(plugin-owned Redis). This module owns the connection: the settings guard is
private to it, and every other module reaches Redis through the context helpers
here.

A session registration is the string key ``channel:web:session:{token}`` holding
the visitor id the cookie token resolves to and the web route identity it was minted
on. Only a REGISTERED token is a session: an unregistered one resolves to nothing, so
an invented or planted cookie value can never become a conversation address; and the
identity is what binds one session to one route — a door refuses a session minted
elsewhere exactly as it refuses an unknown token. A fresh mint gets only
``session_pending_ttl_seconds``; ``resolve_session`` promotes it to the full
``session_ttl_seconds`` (one ``GETEX``), so a cookie that comes back never ages out
mid-conversation while a mint nobody returns with expires in minutes.

The browser-replay transcript is one Redis STREAM per conversation, keyed by the
pair ``(identity, address)`` — the web route identity and the visitor id.
Each entry holds one ready-to-emit SSE frame: an ``event`` name
(``chat.message`` / ``chat.question`` / ``chat.answered``) and a ``data`` field
already ``json.dumps``'d at write, so replay re-emits it verbatim (the JSON
encoding is what stops a newline in a message body from injecting extra frames).
XADD trims to exactly ``transcript_max_entries`` and every append refreshes the
key's TTL (one pipeline) — the durable record of a turn is the conversation
bridge's; this stream is a bounded replay buffer only. ``transcript_order`` is the
write-order gate: the message door holds a conversation's lock from ``accept``
until the visitor's own frame is written, and every agent-side append takes it, so
a reply the accepted turn spawns can never precede the message that caused it.

A pending-question record is a separate string key ``channel:web:question:{id}``
holding the delivery's ``callback_url`` plus the ``(identity, address)`` the
``chat.answered`` frame is later appended to. ``reserve_question`` writes it with a
TTL of the remaining answer budget (an expired question cannot be answered);
``peek_question`` reads it without claiming (the answer door checks ownership
first); ``claim_question`` claims it with ``GETDEL`` (atomic — a duplicate answer
POST gets ``None``); ``restore_question`` puts it back with ``SET NX`` after a
failed forward, refused with a loud log rather than clobbering a newer reservation,
and refused again once the record has already been restored ``max_restores`` times.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_web.settings import WebRedisSettings, web_redis_settings, web_settings

logger = logging.getLogger(__name__)

MESSAGE_EVENT = "chat.message"
QUESTION_EVENT = "chat.question"
ANSWERED_EVENT = "chat.answered"


class SessionRecordError(RuntimeError):
    """A stored session registration is not the shape this module wrote."""


@dataclass(frozen=True)
class SessionRegistration:
    """What one cookie token is registered as: the conversation address every door
    uses for that visitor, and the web route identity the session was minted on. A
    request naming a different identity is not this session's to serve."""

    visitor_id: str
    identity: str


@dataclass(frozen=True)
class QuestionRecord:
    """A pending web question: the callback door to forward the answer to, and the
    transcript pair the ``chat.answered`` frame is appended to on success.

    ``restores`` counts how often a failed forward has already put this record back.
    It rides the record itself so the count expires exactly with the question, and it
    is what bounds a visitor looping an answer the callback door keeps refusing."""

    callback_url: str
    identity: str
    address: str
    timeout_at: datetime
    restores: int = 0


def _session_key(token: str) -> str:
    return f"channel:web:session:{token}"


def _transcript_key(identity: str, address: str) -> str:
    return f"channel:web:transcript:{identity}:{address}"


def _question_key(interaction_id: str) -> str:
    return f"channel:web:question:{interaction_id}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _redis_settings() -> WebRedisSettings:
    """The plugin's store connection, raising a clear config error when unset.

    ``redis_url`` falls back to ``TAI_DEFAULT_REDIS_URL`` via the mixin; the guard
    fires only when NEITHER the channel's own URL nor the shared default is set."""
    settings = web_redis_settings()
    if not settings.redis_url:
        raise ValueError("web channel transcript store is not configured: set CHANNEL_WEB_REDIS_URL.")
    return settings


def _redis():
    return tai42_app.clients.client_ctx(RedisClient, _redis_settings())


def tail_redis_ctx():
    """A FRESH, non-pooled store connection with the socket read timeout stripped —
    the SSE live tail's blocking XREAD, which a blanket read timeout would kill."""
    return tai42_app.clients.client_ctx(
        RedisClient, _redis_settings().model_copy(update={"socket_timeout": None}), fresh=True
    )


def pooled_redis_ctx():
    """A pooled store connection, for a bounded read that releases it immediately."""
    return _redis()


def _decode_session(raw: str | bytes) -> SessionRegistration:
    data = json.loads(raw)
    visitor_id = data.get("visitor_id") if isinstance(data, dict) else None
    identity = data.get("identity") if isinstance(data, dict) else None
    if not isinstance(visitor_id, str) or not visitor_id:
        raise SessionRecordError("a stored web session registration carries no visitor_id")
    if not isinstance(identity, str) or not identity:
        raise SessionRecordError("a stored web session registration carries no identity")
    return SessionRegistration(visitor_id=visitor_id, identity=identity)


async def register_session(token: str, visitor_id: str, identity: str) -> None:
    """Register a freshly minted cookie token against its visitor id and the web route
    it was minted on. Until this lands the token is not a session, so it is written
    BEFORE the cookie is set.

    A mint gets only the SHORT pending TTL: nothing has come back with this cookie
    yet, so an anonymous mint loop leaves keys that expire in minutes rather than one
    full-TTL registration per request. ``resolve_session`` promotes it."""
    settings = web_settings()
    payload = json.dumps({"visitor_id": visitor_id, "identity": identity, "created_at": _now_iso()})
    async with _redis() as redis:
        await redis.set(_session_key(token), payload, ex=settings.session_pending_ttl_seconds)


async def resolve_session(token: str) -> SessionRegistration | None:
    """The registration a cookie token stands for, promoting it to the full session
    TTL (the cookie came back, so this is a real visitor); ``None`` when the token was
    never registered or has expired — both mean "no session". One ``GETEX``: read and
    TTL in a single round trip."""
    settings = web_settings()
    async with _redis() as redis:
        raw = await redis.getex(_session_key(token), ex=settings.session_ttl_seconds)
    if raw is None:
        return None
    return _decode_session(raw)


async def drop_session(token: str) -> None:
    """Delete a registration so its token resolves to nothing (rotation)."""
    async with _redis() as redis:
        await redis.delete(_session_key(token))


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


async def append_question(
    identity: str,
    address: str,
    interaction_id: str,
    question: str,
    answer_format: str,
    options: list[str] | None,
    timeout_at: datetime,
    callback_url: str | None = None,
) -> str:
    """Append one ``chat.question`` entry (the UI renders the per-format widget) and
    return its id.

    ``callback_url`` is a bearer ticket for the interaction, so it is carried in the
    frame ONLY when the widget itself must open it (the ``external`` format);
    otherwise the key is absent and the ticket never leaves the server, where every
    other format answers by interaction id through this plugin's own door."""
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


def _mint_id() -> str:
    return uuid4().hex


def _encode_record(record: QuestionRecord) -> str:
    return json.dumps(
        {
            "callback_url": record.callback_url,
            "identity": record.identity,
            "address": record.address,
            "timeout_at": record.timeout_at.astimezone(UTC).isoformat(),
            "restores": record.restores,
        }
    )


def _decode_record(raw: str | bytes) -> QuestionRecord:
    data = json.loads(raw)
    return QuestionRecord(
        callback_url=data["callback_url"],
        identity=data["identity"],
        address=data["address"],
        timeout_at=datetime.fromisoformat(data["timeout_at"]),
        restores=data["restores"],
    )


def _remaining_seconds(timeout_at: datetime) -> int:
    """Whole seconds until the deadline, raising when it already passed."""
    remaining = math.ceil((timeout_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        raise ChannelDeliveryError(f"question deadline {timeout_at.isoformat()} has already passed")
    return remaining


async def reserve_question(interaction_id: str, record: QuestionRecord) -> None:
    """Store the pending-question record with a TTL of the remaining answer budget."""
    ttl = _remaining_seconds(record.timeout_at)
    async with _redis() as redis:
        await redis.set(_question_key(interaction_id), _encode_record(record), ex=ttl)


async def release_question(interaction_id: str) -> None:
    """Drop a reservation (the transcript append failed — the human never saw it)."""
    async with _redis() as redis:
        await redis.delete(_question_key(interaction_id))


async def peek_question(interaction_id: str) -> QuestionRecord | None:
    """Read the pending record WITHOUT claiming it, so the answer door can refuse a
    question belonging to another conversation before the destructive ``GETDEL``."""
    async with _redis() as redis:
        raw = await redis.get(_question_key(interaction_id))
    if raw is None:
        return None
    return _decode_record(raw)


async def claim_question(interaction_id: str) -> QuestionRecord | None:
    """Atomically claim-and-remove the pending record; ``None`` when there is none
    (``GETDEL`` — a concurrent duplicate answer POST gets ``None``)."""
    async with _redis() as redis:
        raw = await redis.getdel(_question_key(interaction_id))
    if raw is None:
        return None
    return _decode_record(raw)


async def restore_question(interaction_id: str, record: QuestionRecord, max_restores: int) -> bool:
    """Put a claimed record back with its remaining TTL after a failed forward, and
    report whether it is answerable again.

    A ``SET NX``: if a NEW question reserved the id in the gap it is not clobbered
    (refused with a loud log); a record past its deadline is not restored.

    ``max_restores`` bounds the loop: every restore is one more forward a visitor can
    make the server pay for, and the callback door's own rate limit is keyed on this
    server's egress IP and shared with every other channel. Past the cap the record
    stays dropped — loudly — and the interaction resolves by its own timeout."""
    if record.restores >= max_restores:
        logger.error(
            "not restoring the pending question %s: its answer has already been refused %d times "
            "(cap %d); the question is dropped and will resolve by its own timeout",
            interaction_id,
            record.restores,
            max_restores,
        )
        return False
    remaining = math.ceil((record.timeout_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    if remaining <= 0:
        return False
    restored = replace(record, restores=record.restores + 1)
    async with _redis() as redis:
        stored = await redis.set(_question_key(interaction_id), _encode_record(restored), nx=True, ex=remaining)
    if not stored:
        logger.error(
            "could not restore the pending question %s: a new reservation has since taken the id; "
            "the old ask will resolve by its own timeout",
            interaction_id,
        )
        return False
    return True


def _as_str(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value


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
        frame = _decode_entry({_as_str(k): _as_str(v) for k, v in fields.items()})
        if frame is None:
            logger.warning("skipping malformed web transcript backlog entry %s", _as_str(entry_id))
            continue
        frames.append(frame)
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
            frame = _decode_entry({_as_str(k): _as_str(v) for k, v in fields.items()})
            if frame is None:
                logger.warning("skipping malformed web transcript tail entry %s", cursor)
                continue
            frames.append(frame)
    return cursor, frames
