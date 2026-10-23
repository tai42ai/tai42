"""The send record's writes: its begin, its completion or end, the settle and the ask reply.

A send record is written in ``sending`` before its send goes out and ended exactly once, by the
completion of its outcome, by its end (the send raised, passed its bound or was cancelled), or by
the settle of a row whose process stopped before the send answered. Every end runs the shared
placement step (``record_send_scripts._SEND_PLACEMENT_CHECKS`` / ``_SEND_PLACEMENT_WRITES``).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Literal

from tai42_contract.conversations import ConversationRoute
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations import records as _records
from tai42_skeleton.conversations.models import AskReply, ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.persons import thread_door
from tai42_skeleton.conversations.record_keys import _member
from tai42_skeleton.conversations.record_scripts import (
    _F_STATUS,
)
from tai42_skeleton.conversations.record_send_scripts import (
    _ASK_REPLY_LUA,
    _BEGIN_SEND_LUA,
    _COMPLETE_SEND_LUA,
    _END_SEND_LUA,
)
from tai42_skeleton.conversations.record_write import RecordWriteMixin
from tai42_skeleton.utils.redis_typing import awaited, eval_script

logger = logging.getLogger(__name__)

#: The error a settled send record carries.
SETTLE_ERROR = "the process stopped before the medium answered"

#: How many times an ask reply re-reads a row that moved under it before it gives up loudly. A
#: send record moves at most three times (``sending`` → ``provisional`` → ``delivered``/``failed``).
_ASK_REPLY_ATTEMPTS = 5

#: The end states a send record without an outcome takes.
SendEndStatus = Literal[DeliveryStatus.FAILED, DeliveryStatus.OUTCOME_UNKNOWN]


@dataclass(frozen=True)
class SendPlacement:
    """What a send record's completion, end or settle did.

    ``code`` is 1 placed in its thread, 0 recorded while its route was gone (no thread index
    written), -1 removed or gone (``detail`` is ``gone``, ``forgotten`` or ``person_closed``), -2
    the row was no longer ``sending`` (``detail`` is its status). On 1 and 0 ``detail`` is the
    status written.
    """

    code: int
    detail: str


@dataclass(frozen=True)
class SettledSend:
    """A ``sending`` row the settle ended, with what its placement step did."""

    record: ConversationRecord
    placement: SendPlacement


AskReplyOutcome = Literal["recorded", "gone", "unknown"]


def send_placement_warning(record: ConversationRecord, placement: SendPlacement) -> str | None:
    """The WARNING text a send record's placement answer calls for, or ``None`` when it was placed."""
    who = f"send record {record.message_id} on {record.channel} to {record.client_address}"
    if placement.code == 0:
        return f"{who}: its route {record.route_name} was deleted while the send was going out; no thread names it"
    if placement.code != -1:
        return None
    if placement.detail == "forgotten":
        return f"{who}: its thread was forgotten while the send was going out; the record is forgotten with it"
    if placement.detail == "person_closed":
        return (
            f"{who}: its person thread {record.thread_id} takes no new record, its person is being erased or no "
            "longer exists; the record is forgotten"
        )
    return f"{who} is gone; nothing was written"


def _placement(reply: list) -> SendPlacement:
    detail = reply[1]
    return SendPlacement(code=int(reply[0]), detail=detail.decode() if isinstance(detail, bytes) else str(detail))


def _check_route(record: ConversationRecord, route: ConversationRoute) -> None:
    if route.route_name != record.route_name:
        raise ValueError(
            f"send record {record.message_id} names route {record.route_name!r}, written under {route.route_name!r}"
        )


def _unique(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


class RecordSendMixin(RecordWriteMixin):
    """The send record's writes over the shared record layout."""

    def _send_keys(
        self, record: ConversationRecord, target: DeliveryStatus, *, sending_route: str
    ) -> tuple[list[str], bool]:
        """The send layout's keys for placing ``record`` in its thread, and whether it is a person thread."""
        keys = self._record_keys(record.message_id, target, record, route_row=True)
        keys.append(self.settings.route_sending_key(sending_route))
        person_keys = self._person_thread_keys(record.thread_id)
        keys.extend(person_keys)
        return keys, bool(person_keys)

    async def begin_send_record(
        self, record: ConversationRecord, *, route: ConversationRoute, settle_deadline: float
    ) -> None:
        """Write a send record in ``sending`` before its send goes out, on ``route``, the route it resolved.

        Indexed under ``sending`` scored by ``settle_deadline``, added to its route's in-flight set,
        with no TTL until it ends; an ask's record also writes its ask key. A begin into a route-keyed
        thread stamps the writer of its door (:func:`persons.thread_door`) on that thread and keeps
        the door on the row for the send's end. Raises when a row under
        the record's id already exists, so nothing is sent on a record that is not this one.
        """
        if record.origin != "send" or record.delivery_status is not DeliveryStatus.SENDING:
            raise ValueError(
                f"begin_send_record writes a send record in sending, got origin {record.origin!r} "
                f"and status {record.delivery_status.value!r}"
            )
        _check_route(record, route)
        keys = self._record_keys(record.message_id, DeliveryStatus.SENDING)
        keys.append(self.settings.route_sending_key(record.route_name))
        ask_ttl = ""
        if record.ask is not None:
            keys.append(self.settings.ask_send_key(record.ask.interaction_id))
            ask_ttl = str(self.settings.answer_retention_ttl_seconds * 1000)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            written = int(
                await eval_script(
                    r,
                    _BEGIN_SEND_LUA,
                    len(keys),
                    *keys,
                    self._content_blob(record),
                    record.attempts,
                    record.updated_at,
                    record.message_id,
                    settle_deadline,
                    record.thread_id,
                    ask_ttl,
                    thread_door(route, record.thread_id) or "",
                    self.settings.writer_prefixes(),
                )
            )
        if not written:
            raise RuntimeError(f"send record {record.message_id} already exists; nothing was sent")

    async def complete_send_record(self, record: ConversationRecord, *, route: ConversationRoute) -> SendPlacement:
        """Complete a ``sending`` record from its outcome: ``record`` carries the outcome's fields and ``sends``.

        Moves it to ``provisional`` (or ``delivered`` when it already carries an ask reply), writes
        keyspace 3 and the message refs of its sent ids and places it in its thread. ``route`` is
        the route it was begun on, whose in-flight set holds it; a landing in a route-keyed thread
        stamps the writer of the door that route gives the outcome's thread.
        """
        if record.channel is None or record.our_identity is None:
            raise ValueError(f"send record {record.message_id} completes without a channel and our_identity")
        _check_route(record, route)
        now = time.time()
        keys, person = self._send_keys(record, DeliveryStatus.PROVISIONAL, sending_route=route.route_name)
        ids = _unique([pid for sent in record.sends for pid in sent.provider_message_ids])
        keys.extend(self.settings.outbound_index_key(record.channel, pid) for pid in ids)
        keys.extend(
            self.settings.message_ref_key(record.channel, record.our_identity, record.client_address, pid)
            for pid in ids
        )
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            reply = await eval_script(
                r,
                _COMPLETE_SEND_LUA,
                len(keys),
                *keys,
                self._content_blob(record),
                json.dumps([sent.model_dump(mode="json") for sent in record.sends]),
                record.attempts,
                now,
                record.message_id,
                self._index_score(DeliveryStatus.PROVISIONAL, now),
                self._index_score(DeliveryStatus.DELIVERED, now),
                now + self.settings.delivery_grace_seconds,
                self.settings.answer_retention_ttl_seconds * 1000,
                record.thread_id,
                record.created_at,
                record.route_name,
                "1" if person else "",
                len(ids),
                thread_door(route, record.thread_id) or "",
                self.settings.writer_prefixes(),
            )
        return _placement(reply)

    async def end_send_record(self, record: ConversationRecord, *, status: SendEndStatus, error: str) -> SendPlacement:
        """End a ``sending`` record that gave no outcome as ``status`` with ``error``, in its begin thread.

        A record already carrying an ask reply ends ``delivered`` instead, without the error. The
        retention TTL is applied to the row and its ref lists. A landing in a route-keyed begin thread
        stamps the writer of the begin's door.
        """
        if status not in (DeliveryStatus.FAILED, DeliveryStatus.OUTCOME_UNKNOWN):
            raise ValueError(f"a send record ends failed or outcome_unknown, got {status.value!r}")
        now = time.time()
        ended = record.model_copy(update={"delivery_status": status, "error": error, "updated_at": now})
        answered = record.model_copy(update={"delivery_status": DeliveryStatus.DELIVERED, "updated_at": now})
        keys, person = self._send_keys(record, status, sending_route=record.route_name)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            reply = await eval_script(
                r,
                _END_SEND_LUA,
                len(keys),
                *keys,
                self._content_blob(ended),
                self._content_blob(answered),
                status.value,
                now,
                record.message_id,
                self._index_score(status, now),
                self.settings.answer_retention_ttl_seconds * 1000,
                record.thread_id,
                record.created_at,
                record.route_name,
                "1" if person else "",
                self.settings.writer_prefixes(),
            )
        return _placement(reply)

    async def settle_due_sends(self) -> list[SettledSend]:
        """End every ``sending`` row past its settle deadline as ``outcome_unknown``, in its begin thread.

        Such a row belongs to a process that stopped inside its send; it is never re-sent. A row
        gone from under the index is unindexed; one that moved on is left to its writer; an
        unreadable one is moved to terminal ``failed``.
        """
        now = time.time()
        due: list[ConversationRecord] = []
        unreadable: list[str] = []
        key = self.settings.status_index_key(DeliveryStatus.SENDING.value)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            for member in await awaited(r.zrangebyscore(key, "-inf", now)):
                message_id = _member(member)
                hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
                if not hashed:
                    logger.warning("conversations: record %r is indexed but has no row; unindexed", message_id)
                    await awaited(r.zrem(key, message_id))
                    continue
                if hashed.get(_F_STATUS) != DeliveryStatus.SENDING.value:
                    continue
                try:
                    due.append(self._from_hash(hashed))
                except (ValueError, KeyError):
                    logger.warning(
                        "conversations: send record %r is corrupt; moving it to terminal failed in the settle",
                        message_id,
                    )
                    unreadable.append(message_id)
        for message_id in unreadable:
            await self.mark_unreadable(message_id, now)
        settled: list[SettledSend] = []
        for record in due:
            placement = await self.end_send_record(record, status=DeliveryStatus.OUTCOME_UNKNOWN, error=SETTLE_ERROR)
            settled.append(SettledSend(record=record, placement=placement))
        return settled

    async def ask_send_message_id(self, interaction_id: str) -> str | None:
        """The ``message_id`` of the send record that carried the ask, or ``None`` when none names it.

        ``None`` when the ask was sent outside any conversation, or its record expired. The row the
        id names may already be gone (its thread was forgotten); the caller reads it to know.
        """
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            raw = await awaited(r.get(self.settings.ask_send_key(interaction_id)))
        return None if raw is None else _member(raw)

    async def record_ask_reply(self, interaction_id: str, reply: AskReply) -> tuple[AskReplyOutcome, str | None]:
        """Record the participant's channel reply on the ask's send record.

        Answers ``("recorded", message_id)``, ``("gone", message_id)`` when the ask key names a row
        that no longer exists (its thread was forgotten), or ``("unknown", None)`` when no send
        record names the ask. A sensitive ask's reply is recorded without its body. A reply moves a
        ``provisional``, ``failed`` or ``outcome_unknown`` record to ``delivered`` (it proves the
        question arrived), clearing a failed end's error.
        """
        message_id = await self.ask_send_message_id(interaction_id)
        if message_id is None:
            return "unknown", None
        for _attempt in range(_ASK_REPLY_ATTEMPTS):
            record = await self.get_record(message_id)
            if record is None:
                return "gone", message_id
            outcome = await self._write_ask_reply(record, reply)
            if outcome == 1:
                return "recorded", message_id
            if outcome == -1:
                return "gone", message_id
        raise RuntimeError(
            f"ask reply for interaction {interaction_id}: send record {message_id} kept changing state; "
            f"nothing recorded after {_ASK_REPLY_ATTEMPTS} reads"
        )

    async def _write_ask_reply(self, record: ConversationRecord, reply: AskReply) -> int:
        """One compare-and-set write of ``reply`` on ``record`` as read: 1 written, -1 gone, -3 it moved."""
        if record.ask is None:
            raise RuntimeError(f"ask key names record {record.message_id}, which carries no ask")
        if record.ask.sensitive:
            reply = reply.model_copy(update={"answer": None})
        status = record.delivery_status
        if status in (DeliveryStatus.FAILED, DeliveryStatus.OUTCOME_UNKNOWN):
            content = self._content_blob(
                record.model_copy(update={"delivery_status": DeliveryStatus.DELIVERED, "error": None})
            )
        elif status in (DeliveryStatus.SENDING, DeliveryStatus.PROVISIONAL, DeliveryStatus.DELIVERED):
            content = ""
        else:
            raise RuntimeError(f"send record {record.message_id} is in {status.value!r}, a state a send never takes")
        now = time.time()
        keys = self._record_keys(record.message_id, DeliveryStatus.DELIVERED)
        ref = ""
        if reply.medium_message_id is not None:
            if record.channel is None or record.our_identity is None:
                raise ValueError(f"send record {record.message_id} carries no channel and our_identity")
            keys.append(
                self.settings.message_ref_key(
                    record.channel, record.our_identity, record.client_address, reply.medium_message_id
                )
            )
            ref = "1"
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _ASK_REPLY_LUA,
                    len(keys),
                    *keys,
                    reply.model_dump_json(),
                    record.message_id,
                    now,
                    self.settings.answer_retention_ttl_seconds * 1000,
                    self._index_score(DeliveryStatus.DELIVERED, now),
                    status.value,
                    content,
                    ref,
                )
            )
