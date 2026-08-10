"""The answer/record store — keyspaces 1, 2, 3, 6, 7 and 8 of the conversation bridge, all
transient runtime state (NOT a backup section) and all Redis-backed:

1. Inbound dedupe: ``conversations:dedupe:{channel}:{provider_message_id}`` → the
   ``message_id`` that first claimed the pair. The claim has no release path, so it is
   taken only once a durable record already stands behind it.
2. Answer record: ``conversations:record:{message_id}`` — intake, produced answer and
   delivery state, split into a content blob plus the delivery-control fields the atomic
   transitions mutate. The retention TTL is applied ONLY on reaching a terminal state.
3. Outbound-id reverse index: ``conversations:outbound:{channel}:{outbound_id}`` →
   ``message_id``, so an out-of-band receipt resolves back to its record.
6. Per-status record index: ``conversations:status:{delivery_status}`` → the ``message_id``s
   in that state, moved by the same atomic step that moves the record. Every listing reads
   it, so a sweep costs the work outstanding and not the whole retained keyspace.
7. Per-thread transcript index: ``conversations:thread:{route_name}:{thread_id}`` → the
   thread's ``message_id``s scored by ``created_at``, written by the same atomic step that
   creates the record, so a transcript reads in the order the messages were sent.
8. Per-route thread index: ``conversations:route_threads:{route_name}`` → the route's
   ``thread_id``s scored by the moment each was last active, so the monitoring listing
   reads the newest first.

Indexes 7 and 8 name rows that expire under the retention TTL. Reclaiming those members is
:meth:`ConversationRecordStore.prune_expired_terminal_indexes`'s job ALONE: a read logs the
orphan it walks over and returns the page it was asked for, because a read that mutated the
index it is paging would drop rows out from under its own offsets and compute ``next_page``
from a total it had already changed.

That pass walks LIVE routes only, so nothing may leave either index standing for a route
that no longer routes: a create writes them only while the routing row stands (keyspace 4,
the one key here read and never written), a completion re-stamps index 8 only while index 7
still holds members, and a route's delete reclaims both — resumably, index 8 being the
durable marker of a reclamation that was interrupted.

Every exactly-once transition is a single Lua step guarded on the record's current
``delivery_status``, so racing writers produce ONE outcome, never two.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass

from redis.asyncio import Redis as AsyncRedis
from tai42_contract.conversations import DeliveryReceipt
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations.models import TERMINAL_STATUSES, ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.utils.redis_typing import awaited, eval_script

logger = logging.getLogger(__name__)

_NO_BACKEND = "conversation answer records require the redis conversations backend"

# Hash field names on a record key: ``data`` is the content JSON, the rest are the
# delivery-control fields the atomic transitions mutate.
_F_DATA = "data"
_F_STATUS = "delivery_status"
_F_OUTBOUND = "outbound_ids"
_F_ATTEMPTS = "attempts"
_F_GRACE = "grace_deadline"
_F_UPDATED = "updated_at"
_F_INTAKE = "intake_claim"

# Every record-mutating script takes the same key layout: KEYS[1]=record key, then one
# key per DeliveryStatus (the per-status indexes), then the index the transition moves the
# record INTO. The status count is derived below, so the layout tracks the enum. A script
# that also touches the thread indexes carries them LAST — one slot earlier in the delete,
# which writes no target index — and the create carries the ROUTING ROW behind them, the
# key it decides against whether the route still routes.
_INDEXED_STATUSES = tuple(DeliveryStatus)
_TARGET_INDEX_KEY = f"KEYS[{2 + len(_INDEXED_STATUSES)}]"
_THREAD_INDEX_KEY = f"KEYS[{3 + len(_INDEXED_STATUSES)}]"
_ROUTE_THREADS_KEY = f"KEYS[{4 + len(_INDEXED_STATUSES)}]"
_ROUTE_ROW_KEY = f"KEYS[{5 + len(_INDEXED_STATUSES)}]"
_DELETE_THREAD_INDEX_KEY = f"KEYS[{2 + len(_INDEXED_STATUSES)}]"
_DELETE_ROUTE_THREADS_KEY = f"KEYS[{3 + len(_INDEXED_STATUSES)}]"

# A live record's index member outlives nothing; a terminal one expires with its row.
_NO_EXPIRY_SCORE = "+inf"

#: Newest members of a thread the listing will read for a summary before giving up on it.
#: A thread whose newest members are all rowless is being reclaimed, not displayed.
_THREAD_SUMMARY_SCAN = 20

#: Members of one thread's index, and threads of one route's index, a prune pass reads in
#: ONE command. Both bound a single reply, so no pass ever asks Redis for a whole index.
_PRUNE_MEMBERS_PER_BATCH = 200
_PRUNE_THREADS_PER_BATCH = 200

#: Units of work one prune pass spends. A thread costs one unit to EXAMINE plus one per
#: candidate member it offers, so a route of a hundred thousand HEALTHY threads is bounded
#: too; what is left over drains on the passes that follow.
_PRUNE_WORK_PER_PASS = 5_000


def _member(value: str | bytes) -> str:
    """A sorted-set member as text, whichever form the client is decoding into."""
    return value.decode() if isinstance(value, bytes) else value


def _reindex(member_argv: str, score_argv: str) -> str:
    """Lua moving the record's id into the target status index and out of every other, so
    exactly one index names it and a listing never walks the record keyspace."""
    return f"""
for i = 2, {1 + len(_INDEXED_STATUSES)} do redis.call('ZREM', KEYS[i], {member_argv}) end
redis.call('ZADD', {_TARGET_INDEX_KEY}, {score_argv}, {member_argv})
"""


# Atomic get-or-set of the inbound-dedupe marker: returns the message_id owning the pair —
# the caller's on a fresh claim, the prior turn's on a redelivery.
# KEYS[1]=dedupe key; ARGV = message_id, ttl_seconds.
_CLAIM_INBOUND_LUA = """
-- conversations:dedupe:claim
local existing = redis.call('GET', KEYS[1])
if existing then return existing end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return ARGV[1]
"""

# Write a freshly minted record, applying the retention TTL in the SAME step when it is
# created already terminal so such a record can never be left without one. An ``accepted``
# record is created ALREADY holding its intake lease, so no window exists in which a live
# turn looks stranded. The thread indexes are written in the same step, so no record can
# stand outside the transcript it belongs to — but ONLY while the routing row still stands:
# a door resolves its route a round trip before it lands here, and a delete completing in
# that window has already reclaimed both indexes, so writing them would re-create a pair
# for a route that no longer routes. Returns 1 when the thread indexes were written, 0 when
# the route was gone. ARGV = content_json, delivery_status, outbound_json, attempts,
# updated_at, ttl_ms ('' for a record that must not expire yet), intake_claim ('' off the
# intake path), message_id, index_score, thread_id, created_at.
_CREATE_LUA = f"""
-- conversations:record:create
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'delivery_status', ARGV[2], 'outbound_ids', ARGV[3],
  'attempts', ARGV[4], 'claim', '', 'grace_deadline', '', 'updated_at', ARGV[5], 'intake_claim', ARGV[7])
if ARGV[6] ~= '' then redis.call('PEXPIRE', KEYS[1], ARGV[6]) end
{_reindex("ARGV[8]", "ARGV[9]")}
if redis.call('EXISTS', {_ROUTE_ROW_KEY}) == 0 then return 0 end
redis.call('ZADD', {_THREAD_INDEX_KEY}, ARGV[11], ARGV[8])
redis.call('ZADD', {_ROUTE_THREADS_KEY}, ARGV[11], ARGV[10])
return 1
"""

# Re-stamp the thread's last-activity moment on the route index, but ONLY while the
# thread's own index still holds members. A route delete reclaims both indexes; a turn that
# was already in flight completes after it, and an unconditional stamp would re-create the
# route index holding a thread whose transcript index is empty — a pair no read reaches, no
# TTL expires and the prune (which walks LIVE routes only) never sees.
_RESTAMP_LIVE_THREAD = f"""
if redis.call('ZCARD', {_THREAD_INDEX_KEY}) > 0 then
  redis.call('ZADD', {_ROUTE_THREADS_KEY}, ARGV[2], ARGV[{{slot}}])
end
"""

# Move an intake record from ``accepted`` to ``pending_delivery`` carrying the turn's
# outcome, releasing the intake lease: 1 transitioned, 0 no longer at intake, -1 gone.
# Guarded on the current status, so a finishing turn and a re-drive produce ONE outcome.
# ARGV = content_json, now, message_id, index_score, thread_id.
_COMPLETE_TURN_LUA = f"""
-- conversations:record:complete_turn
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status ~= 'accepted' then return 0 end
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'delivery_status', 'pending_delivery', 'updated_at', ARGV[2],
  'intake_claim', '')
{_reindex("ARGV[3]", "ARGV[4]")}
{_RESTAMP_LIVE_THREAD.format(slot=5)}
return 1
"""

# Move an intake record from ``accepted`` straight to terminal ``silent`` — a tool turn
# whose reply mapped to nothing, so nothing is ever sent — releasing the intake lease and
# applying the retention TTL in the SAME step: 1 transitioned, 0 no longer at intake, -1
# gone. Guarded on the current status, so a finishing turn and a re-drive produce ONE
# outcome. ARGV = content_json, now, ttl_ms, message_id, index_score, thread_id.
_COMPLETE_SILENT_LUA = f"""
-- conversations:record:complete_silent
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status ~= 'accepted' then return 0 end
redis.call('HSET', KEYS[1], 'data', ARGV[1], 'delivery_status', 'silent', 'updated_at', ARGV[2],
  'intake_claim', '', 'claim', '')
redis.call('PEXPIRE', KEYS[1], ARGV[3])
{_reindex("ARGV[4]", "ARGV[5]")}
{_RESTAMP_LIVE_THREAD.format(slot=6)}
return 1
"""

# Take (or refresh) the intake lease under a worker token — the liveness marker a running
# turn holds: 1 held, 0 a DIFFERENT worker's lease is still live, -1 gone, -2 the record
# has left intake. Claim value is ``token:expiry``. The holder refreshes; a re-drive may
# adopt only a LAPSED lease. KEYS[1]=record key; ARGV = now, lease_seconds, token.
_CLAIM_INTAKE_LUA = """
-- conversations:record:intake_claim
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status ~= 'accepted' then return -2 end
local claim = redis.call('HGET', KEYS[1], 'intake_claim')
local now = tonumber(ARGV[1])
if claim and claim ~= '' then
  local sep = string.find(claim, ':')
  local ctoken = string.sub(claim, 1, sep - 1)
  local cexp = tonumber(string.sub(claim, sep + 1))
  if ctoken ~= ARGV[3] and cexp > now then return 0 end
end
redis.call('HSET', KEYS[1], 'intake_claim', ARGV[3] .. ':' .. tostring(now + tonumber(ARGV[2])))
return 1
"""

# Take (or refresh) the exactly-once delivery lease under a worker token: 1 won, 0 the
# record is already sent (provisional) or terminal or a DIFFERENT worker holds a live lease,
# -1 gone, -2 still at intake and carrying no answer. Only a pending_delivery record is
# claimable for a send; a provisional one awaits a receipt, not a re-send. The holder may
# re-claim to extend; anyone else waits for expiry. Claim value is ``token:expiry``.
# KEYS[1]=record key; ARGV = now, lease_seconds, token.
_CLAIM_DELIVERY_LUA = """
-- conversations:record:claim
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status == 'accepted' then return -2 end
if status ~= 'pending_delivery' then return 0 end
local claim = redis.call('HGET', KEYS[1], 'claim')
local now = tonumber(ARGV[1])
if claim and claim ~= '' then
  local sep = string.find(claim, ':')
  local ctoken = string.sub(claim, 1, sep - 1)
  local cexp = tonumber(string.sub(claim, sep + 1))
  if ctoken ~= ARGV[3] and cexp > now then return 0 end
end
redis.call('HSET', KEYS[1], 'claim', ARGV[3] .. ':' .. tostring(now + tonumber(ARGV[2])))
return 1
"""


def _foreign_lease_guard(now_argv: str, token_argv: str) -> str:
    """Lua that returns -3 when a DIFFERENT worker holds a live delivery lease — the check
    every delivery-state write takes before it mutates, so a worker whose lease lapsed and
    was taken over cannot overwrite the holder's progress."""
    return f"""
local claim = redis.call('HGET', KEYS[1], 'claim')
if claim and claim ~= '' then
  local sep = string.find(claim, ':')
  if string.sub(claim, 1, sep - 1) ~= {token_argv}
     and tonumber(string.sub(claim, sep + 1)) > tonumber({now_argv}) then
    return -3
  end
end
"""


# Move a record to ``provisional``, recording the outbound ids and grace deadline and
# releasing the lease: 1 transitioned, 0 already terminal, -1 gone, -3 a foreign live lease.
# ARGV = outbound_json, attempts, grace_deadline, now, token, message_id, index_score.
_PROVISIONAL_LUA = f"""
-- conversations:record:provisional
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status == 'delivered' or status == 'failed' or status == 'shed' then return 0 end
{_foreign_lease_guard("ARGV[4]", "ARGV[5]")}
redis.call('HSET', KEYS[1], 'delivery_status', 'provisional', 'outbound_ids', ARGV[1],
  'attempts', ARGV[2], 'grace_deadline', ARGV[3], 'updated_at', ARGV[4], 'claim', '')
{_reindex("ARGV[6]", "ARGV[7]")}
return 1
"""

# Terminal delivered write from the send path: 1 transitioned, 0 already delivered
# (idempotent), -1 gone, -2 already failed (a conflict the caller logs), -3 a foreign live
# lease. Sets the retention TTL.
# ARGV = outbound_json, attempts, now, ttl_ms, token, message_id, index_score.
_DELIVERED_LUA = f"""
-- conversations:record:delivered
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status == 'failed' or status == 'shed' then return -2 end
if status == 'delivered' then return 0 end
{_foreign_lease_guard("ARGV[3]", "ARGV[5]")}
redis.call('HSET', KEYS[1], 'delivery_status', 'delivered', 'outbound_ids', ARGV[1],
  'attempts', ARGV[2], 'updated_at', ARGV[3], 'claim', '', 'grace_deadline', '')
redis.call('PEXPIRE', KEYS[1], ARGV[4])
{_reindex("ARGV[6]", "ARGV[7]")}
return 1
"""

# Terminal failed write from the send path (retries exhausted): 1 transitioned, 0 already
# failed, -1 gone, -2 the send already completed (delivered/shed/provisional), -3 a foreign
# live lease. Sets the retention TTL.
# ARGV = attempts, now, ttl_ms, token, message_id, index_score.
_FAILED_LUA = f"""
-- conversations:record:failed
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
if status == 'delivered' or status == 'shed' or status == 'provisional' then return -2 end
if status == 'failed' then return 0 end
{_foreign_lease_guard("ARGV[2]", "ARGV[4]")}
redis.call('HSET', KEYS[1], 'delivery_status', 'failed', 'attempts', ARGV[1],
  'updated_at', ARGV[2], 'claim', '', 'grace_deadline', '')
redis.call('PEXPIRE', KEYS[1], ARGV[3])
{_reindex("ARGV[5]", "ARGV[6]")}
return 1
"""

# Ingest an out-of-band receipt against a fully sent (``provisional``) record: 1
# transitioned, 0 already in the target terminal state, -1 gone, -2 conflicting terminal
# state, -3 the send has not finished. ARGV = target_status, now, ttl_ms, message_id,
# index_score.
_RECEIPT_LUA = f"""
-- conversations:record:receipt
local status = redis.call('HGET', KEYS[1], 'delivery_status')
if not status then return -1 end
local target = ARGV[1]
if status == target then return 0 end
if status == 'delivered' or status == 'failed' or status == 'shed' then return -2 end
if status ~= 'provisional' then return -3 end
redis.call('HSET', KEYS[1], 'delivery_status', target, 'updated_at', ARGV[2],
  'claim', '', 'grace_deadline', '')
redis.call('PEXPIRE', KEYS[1], ARGV[3])
{_reindex("ARGV[4]", "ARGV[5]")}
return 1
"""


# Delete a record and its index membership in ONE step, so no listing can name a row that
# is gone. The thread leaves the route index with its LAST record, never before: the other
# records of a thread outlive any one of them. Returns the number of keys removed.
# ARGV = message_id, thread_id.
_DELETE_LUA = f"""
-- conversations:record:delete
local removed = redis.call('DEL', KEYS[1])
for i = 2, {1 + len(_INDEXED_STATUSES)} do redis.call('ZREM', KEYS[i], ARGV[1]) end
redis.call('ZREM', {_DELETE_THREAD_INDEX_KEY}, ARGV[1])
if redis.call('ZCARD', {_DELETE_THREAD_INDEX_KEY}) == 0 then
  redis.call('ZREM', {_DELETE_ROUTE_THREADS_KEY}, ARGV[2])
end
return removed
"""

# Unindex a member whose row is already gone — an orphan a status listing found. It names
# no thread (a vanished row cannot say which), so the thread indexes are reclaimed by the
# prune pass instead. Returns the number of index members removed.
# ARGV = message_id.
_UNINDEX_LUA = f"""
-- conversations:record:unindex
local removed = 0
for i = 2, {1 + len(_INDEXED_STATUSES)} do removed = removed + redis.call('ZREM', KEYS[i], ARGV[1]) end
return removed
"""

# Reclaim one thread's expired members and, once nothing is left, the thread itself — in ONE
# step, so a create landing mid-prune is never clobbered: it either precedes the ZCARD (which
# then sees its member) or follows the whole script (and re-adds the thread). Called with NO
# candidate too, which is how a thread whose index is already empty leaves the route index.
# KEYS[1]=thread index, KEYS[2]=route thread index, KEYS[3..]=one record key per candidate;
# ARGV[1]=thread_id, ARGV[2..]=the candidates' message_ids, parallel to KEYS[3..]. Returns
# ``{members removed, members the thread index still holds}`` — the walker needs the second
# number to know whether this thread just left the route index and so shifted the ranks of
# the threads behind it.
_PRUNE_THREAD_LUA = """
-- conversations:thread:prune
local removed = 0
for i = 3, #KEYS do
  if redis.call('EXISTS', KEYS[i]) == 0 then
    removed = removed + redis.call('ZREM', KEYS[1], ARGV[i - 1])
  end
end
local remaining = redis.call('ZCARD', KEYS[1])
if remaining == 0 then redis.call('ZREM', KEYS[2], ARGV[1]) end
return {removed, remaining}
"""


@dataclass(frozen=True)
class ThreadSummary:
    """One thread as the route's thread listing shows it — its newest record's address,
    state and moment, plus how many records the thread still holds."""

    thread_id: str
    client_address: str
    last_activity_at: float
    message_count: int
    last_delivery_status: DeliveryStatus


@dataclass(frozen=True)
class ThreadPage:
    """One page of a route's threads, newest activity first, with the indexed total the
    page was cut from."""

    threads: list[ThreadSummary]
    total: int


@dataclass(frozen=True)
class TranscriptPage:
    """One page of a thread's records in the direction the read asked for — oldest first by
    default, newest first under ``newest_first`` — with the thread's indexed total. A
    ``total`` of 0 is a thread the index no longer holds at all."""

    records: list[ConversationRecord]
    total: int


@dataclass(frozen=True)
class PruneCursor:
    """Where a prune pass stopped, so the next one RESUMES there instead of re-reading the
    same head of the same index forever.

    A record carries no expiry until it turns terminal, so an index member can be older
    than the retention window while its row is still very much alive. A run of such members
    at the front of a thread — or a run of such threads at the front of a route — would
    absorb every pass's budget and the reclaimable members behind them would never be
    reached. The cursor is what turns that live-lock into forward progress.

    ``route_name`` is the route the pass stopped in, ``thread_rank`` the rank it stopped at
    in that route's thread index, and ``member_rank`` the rank it stopped at inside that
    thread's own index. A cursor naming a route that no longer exists simply starts the
    next pass from the beginning.

    ``thread_id`` pins ``member_rank`` to the thread it was measured in. A thread that
    leaves the route index between two passes shifts every thread behind it down one rank,
    so the rank alone would carry a member offset into a DIFFERENT thread and silently skip
    that thread's first members; the next pass applies ``member_rank`` only when the thread
    standing at ``thread_rank`` is still this one."""

    route_name: str | None = None
    thread_rank: int = 0
    member_rank: int = 0
    thread_id: str | None = None


#: The cursor a pass starts (and wraps) at: the first thread of the first route.
PRUNE_START = PruneCursor()


def _resume_at(route_names: list[str], resume: str | None) -> list[str]:
    """``route_names`` rotated so the route the last pass stopped in comes first, and the
    routes it already walked are re-reached at the end of this one. An unknown (deleted)
    resume point falls back to the given order."""
    if resume is None or resume not in route_names:
        return route_names
    at = route_names.index(resume)
    return route_names[at:] + route_names[:at]


@dataclass(frozen=True)
class _ThreadPruneStep:
    """What draining one thread cost and where it left off. ``resume_rank`` is ``None``
    exactly when the thread has no expired candidate left to offer; ``emptied`` says the
    thread's index ran empty, which took it out of the route index and shifted the ranks of
    every thread behind it down by one."""

    spent: int
    resume_rank: int | None
    emptied: bool


@dataclass(frozen=True)
class PendingWork:
    """A non-terminal record a delivery pass found, with the control fields it needs to
    decide the next move."""

    message_id: str
    delivery_status: DeliveryStatus
    attempts: int
    grace_deadline: float | None


class ConversationRecordStore:
    """The Redis-backed answer/record store (keyspaces 1, 2, 3, 6, 7 and 8). Construction
    refuses with a loud 501 without the redis conversations backend — nothing here may be
    persisted to state that vanishes with the process."""

    def __init__(self, settings: ConversationsSettings) -> None:
        if settings.in_memory:
            raise NotSupportedError(_NO_BACKEND)
        self.settings = settings

    # -- the key layout every record-mutating script takes --------------------

    def _record_keys(
        self,
        message_id: str,
        target: DeliveryStatus | None = None,
        thread: ConversationRecord | None = None,
        *,
        route_row: bool = False,
    ) -> list[str]:
        """``[record key, every status index, the target status index, the record's two
        thread indexes, the routing row]``. ``target`` is omitted only by a script that
        writes no status; ``thread`` only by one that touches no thread index;
        ``route_row`` is taken only by the create, which decides against it whether the
        route still routes."""
        keys = [self.settings.record_key(message_id)]
        keys.extend(self.settings.status_index_key(status.value) for status in _INDEXED_STATUSES)
        if target is not None:
            keys.append(self.settings.status_index_key(target.value))
        if thread is not None:
            keys.append(self.settings.thread_index_key(thread.route_name, thread.thread_id))
            keys.append(self.settings.route_threads_key(thread.route_name))
            if route_row:
                keys.append(self.settings.route_key(thread.route_name))
        return keys

    def _index_score(self, status: DeliveryStatus, now: float) -> str:
        """The index member's expiry: a terminal row's member is swept with the row it
        names, a live one's is never swept."""
        if status not in TERMINAL_STATUSES:
            return _NO_EXPIRY_SCORE
        return str(now + self.settings.answer_retention_ttl_seconds)

    # -- inbound dedupe (keyspace 1) -----------------------------------------

    async def get_inbound_owner(self, channel: str, provider_message_id: str) -> str | None:
        """The ``message_id`` owning ``(channel, provider_message_id)``, or ``None`` when
        unclaimed — a door's redelivery fast path. It claims nothing, so
        :meth:`claim_inbound` stays the authority concurrent accepts arbitrate through."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            owner = await awaited(r.get(self.settings.dedupe_key(channel, provider_message_id)))
        if owner is None:
            return None
        return owner.decode() if isinstance(owner, bytes) else owner

    async def claim_inbound(self, channel: str, provider_message_id: str, message_id: str) -> str:
        """Claim ``(channel, provider_message_id)`` for ``message_id``, returning the
        ``message_id`` that OWNS the pair — the passed one on a fresh claim, the prior
        turn's on a redelivery. The claim has no release path, so the caller must take it
        only once its record is durably persisted, and discard that record on a loss."""
        key = self.settings.dedupe_key(channel, provider_message_id)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            owner = await eval_script(
                r, _CLAIM_INBOUND_LUA, 1, key, message_id, self.settings.inbound_dedupe_ttl_seconds
            )
        return owner.decode() if isinstance(owner, bytes) else owner

    # -- answer record (keyspace 2) ------------------------------------------

    def _content_blob(self, record: ConversationRecord) -> str:
        """The record's content JSON — every field except the delivery-control ones, which
        live in their own hash fields.

        ``allow_nan=False``: a non-finite float renders as bare ``Infinity``/``NaN``, which
        is not JSON and which no standard parser reads back, so the row would persist
        unreadable. It raises here, at the write, instead."""
        content = record.model_dump(mode="json")
        for control in ("delivery_status", "outbound_message_ids", "attempts", "updated_at"):
            content.pop(control, None)
        return json.dumps(content, allow_nan=False)

    async def create_record(self, record: ConversationRecord, *, intake_token: str | None = None) -> None:
        """Persist a freshly minted record in the state it carries (always a create — the
        ``message_id`` is a fresh uuid4). A non-terminal record carries NO expiry until it
        reaches a terminal state; one created already terminal gets the retention TTL.

        An ``accepted`` record REQUIRES ``intake_token`` and is created already holding that
        worker's intake lease; any other state requires none.

        The thread indexes are written only while the record's route still routes: a door
        resolves its route a round trip before this write, and a delete completing in that
        window has already reclaimed both indexes. The record still stands (its delivery is
        the sender's to finish), but no transcript names it — logged loudly, because a
        message answered against a route that vanished mid-turn is an operator's business."""
        if (record.delivery_status is DeliveryStatus.ACCEPTED) is not (intake_token is not None):
            raise ValueError(
                "an accepted record is created holding an intake lease and any other state without one; got "
                f"{record.delivery_status.value!r} with intake_token={intake_token!r}"
            )
        now = time.time()
        terminal = record.delivery_status in TERMINAL_STATUSES
        ttl_ms = self.settings.answer_retention_ttl_seconds * 1000 if terminal else ""
        lease_until = now + self.settings.intake_claim_lease_seconds
        keys = self._record_keys(record.message_id, record.delivery_status, record, route_row=True)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            indexed = int(
                await eval_script(
                    r,
                    _CREATE_LUA,
                    len(keys),
                    *keys,
                    self._content_blob(record),
                    record.delivery_status.value,
                    json.dumps(record.outbound_message_ids),
                    record.attempts,
                    record.updated_at,
                    ttl_ms,
                    f"{intake_token}:{lease_until}" if intake_token is not None else "",
                    record.message_id,
                    self._index_score(record.delivery_status, now),
                    record.thread_id,
                    record.created_at,
                )
            )
        if not indexed:
            logger.warning(
                "conversations: route %r stopped routing while record %r was being accepted; the record stands "
                "but its thread indexes were not written, so no transcript names it",
                record.route_name,
                record.message_id,
            )

    async def complete_turn(self, record: ConversationRecord) -> int:
        """Move an intake record from ``accepted`` to ``pending_delivery`` carrying its
        turn's outcome: 1 transitioned, 0 no longer at intake, -1 gone. Guarded on the
        current status, so a late turn and a re-drive cannot both write an outcome."""
        if record.delivery_status is not DeliveryStatus.PENDING_DELIVERY:
            raise ValueError(f"complete_turn writes a pending_delivery record, got {record.delivery_status.value!r}")
        keys = self._record_keys(record.message_id, DeliveryStatus.PENDING_DELIVERY, record)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _COMPLETE_TURN_LUA,
                    len(keys),
                    *keys,
                    self._content_blob(record),
                    record.updated_at,
                    record.message_id,
                    self._index_score(DeliveryStatus.PENDING_DELIVERY, record.updated_at),
                    record.thread_id,
                )
            )

    async def complete_silent(self, record: ConversationRecord) -> int:
        """Move an intake record from ``accepted`` straight to terminal ``silent`` — a tool
        turn that produced no reply — applying the retention TTL: 1 transitioned, 0 no
        longer at intake, -1 gone. Guarded on the current status, so a late turn and a
        re-drive cannot both write an outcome."""
        if record.delivery_status is not DeliveryStatus.SILENT:
            raise ValueError(f"complete_silent writes a silent record, got {record.delivery_status.value!r}")
        # One fresh ``now`` feeds both the index member's expiry score and the ``updated_at``
        # field, so the member's score tracks the row's own TTL clock — the sibling terminal
        # writes' idiom.
        now = time.time()
        keys = self._record_keys(record.message_id, DeliveryStatus.SILENT, record)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _COMPLETE_SILENT_LUA,
                    len(keys),
                    *keys,
                    self._content_blob(record),
                    now,
                    self.settings.answer_retention_ttl_seconds * 1000,
                    record.message_id,
                    self._index_score(DeliveryStatus.SILENT, now),
                    record.thread_id,
                )
            )

    async def claim_intake(self, message_id: str, now: float, token: str, lease_seconds: float) -> int:
        """Take (or refresh) the intake lease on ``message_id`` under ``token``, leased for
        ``lease_seconds``: 1 when held, 0 when a DIFFERENT worker's lease is still live, -1
        when the record is gone, -2 when it has left intake. The worker running the turn
        refreshes its own lease; a re-drive may adopt only a LAPSED one, so a live turn is
        never reaped."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _CLAIM_INTAKE_LUA,
                    1,
                    self.settings.record_key(message_id),
                    now,
                    lease_seconds,
                    token,
                )
            )

    async def delete_record(self, record: ConversationRecord) -> bool:
        """Delete a record and its index membership outright, returning whether one was
        removed — the abort path for an accept that lost the inbound claim and so owns
        nothing. Takes the record, not its id: the thread it is indexed under is named by
        the record alone."""
        keys = self._record_keys(record.message_id, thread=record)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return bool(await eval_script(r, _DELETE_LUA, len(keys), *keys, record.message_id, record.thread_id))

    async def get_record(self, message_id: str) -> ConversationRecord | None:
        """The record for ``message_id``, or ``None`` when no such record exists."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
        if not hashed:
            return None
        return self._from_hash(hashed)

    def _from_hash(self, hashed: dict[str, str]) -> ConversationRecord:
        data = json.loads(hashed[_F_DATA])
        data[_F_STATUS] = hashed[_F_STATUS]
        data["outbound_message_ids"] = json.loads(hashed[_F_OUTBOUND])
        data[_F_ATTEMPTS] = int(hashed[_F_ATTEMPTS])
        data[_F_UPDATED] = float(hashed[_F_UPDATED])
        return ConversationRecord.model_validate(data)

    async def claim_delivery(self, message_id: str, now: float, token: str, lease_seconds: float) -> int:
        """Take (or refresh) the exactly-once delivery lease on ``message_id`` under
        ``token``, leased for ``lease_seconds``: 1 when won, 0 when the record is already
        sent (provisional) or terminal or a different worker holds a live lease, -1 when the
        record is gone, -2 when it is still at intake and carries no answer. Only a
        ``pending_delivery`` record is claimable for a send. The token holder re-claiming
        extends its own lease; a different token waits for expiry."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _CLAIM_DELIVERY_LUA,
                    1,
                    self.settings.record_key(message_id),
                    now,
                    lease_seconds,
                    token,
                )
            )

    async def bump_attempt(self, message_id: str) -> int:
        """Increment and return the record's attempt count — one per send attempt."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(await awaited(r.hincrby(self.settings.record_key(message_id), _F_ATTEMPTS, 1)))

    async def mark_provisional(
        self, message_id: str, outbound_ids: list[str], attempts: int, now: float, token: str
    ) -> int:
        """Move ``message_id`` to ``provisional`` awaiting an async delivery receipt or
        grace expiry: 1 transitioned, 0 already terminal, -1 gone, -3 a different worker's
        live lease. ``token`` is the delivery lease this caller holds."""
        grace_deadline = now + self.settings.delivery_grace_seconds
        keys = self._record_keys(message_id, DeliveryStatus.PROVISIONAL)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _PROVISIONAL_LUA,
                    len(keys),
                    *keys,
                    json.dumps(outbound_ids),
                    attempts,
                    grace_deadline,
                    now,
                    token,
                    message_id,
                    self._index_score(DeliveryStatus.PROVISIONAL, now),
                )
            )

    async def mark_delivered(
        self, message_id: str, outbound_ids: list[str], attempts: int, now: float, token: str
    ) -> int:
        """Terminal delivered write with the retention TTL: 1 transitioned, 0 already
        delivered, -1 gone, -2 already failed, -3 a different worker's live lease.
        ``token`` is the delivery lease this caller holds."""
        keys = self._record_keys(message_id, DeliveryStatus.DELIVERED)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _DELIVERED_LUA,
                    len(keys),
                    *keys,
                    json.dumps(outbound_ids),
                    attempts,
                    now,
                    self.settings.answer_retention_ttl_seconds * 1000,
                    token,
                    message_id,
                    self._index_score(DeliveryStatus.DELIVERED, now),
                )
            )

    async def mark_failed(self, message_id: str, attempts: int, now: float, token: str) -> int:
        """Terminal failed write with the retention TTL: 1 transitioned, 0 already failed,
        -1 gone, -2 the send already completed (delivered/shed/provisional), -3 a different
        worker's live lease. ``token`` is the delivery lease this caller holds."""
        keys = self._record_keys(message_id, DeliveryStatus.FAILED)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _FAILED_LUA,
                    len(keys),
                    *keys,
                    attempts,
                    now,
                    self.settings.answer_retention_ttl_seconds * 1000,
                    token,
                    message_id,
                    self._index_score(DeliveryStatus.FAILED, now),
                )
            )

    async def ingest_receipt(self, message_id: str, receipt: DeliveryReceipt, now: float) -> int:
        """Ingest an out-of-band receipt against a fully sent (``provisional``) record: 1
        transitioned, 0 already in the receipt's terminal state, -1 gone, -2 a conflicting
        terminal state already recorded, -3 the record's send has not finished."""
        target = DeliveryStatus.DELIVERED if receipt is DeliveryReceipt.DELIVERED else DeliveryStatus.FAILED
        keys = self._record_keys(message_id, target)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _RECEIPT_LUA,
                    len(keys),
                    *keys,
                    target.value,
                    now,
                    self.settings.answer_retention_ttl_seconds * 1000,
                    message_id,
                    self._index_score(target, now),
                )
            )

    async def _indexed_ids(self, r: AsyncRedis, statuses: frozenset[DeliveryStatus], now: float) -> list[str]:
        """The ``message_id``s indexed under ``statuses``, dropping the members whose row
        has expired out from under the index first."""
        ids: list[str] = []
        for status in statuses:
            key = self.settings.status_index_key(status.value)
            await awaited(r.zremrangebyscore(key, "-inf", now))
            for member in await awaited(r.zrange(key, 0, -1)):
                ids.append(member.decode() if isinstance(member, bytes) else member)
        return ids

    async def _drop_orphan(self, r: AsyncRedis, message_id: str) -> None:
        """Unindex a status-index member whose row is gone — a row deleted from under the
        index rather than through :meth:`delete_record`."""
        logger.warning("conversations: record %r is indexed but has no row; unindexed", message_id)
        keys = self._record_keys(message_id)
        await eval_script(r, _UNINDEX_LUA, len(keys), *keys, message_id)

    async def pending_work(self) -> list[PendingWork]:
        """Every record the DELIVERY machine has unfinished work on — the listing behind
        the boot re-drive and the periodic sweep. Read from the status index, so it costs
        the work outstanding and not the whole retained keyspace. Terminal and intake
        records are not read (an intake record is the turn engine's to resolve); a corrupt
        row is logged and skipped rather than crashing the pass."""
        work: list[PendingWork] = []
        wanted = frozenset({DeliveryStatus.PENDING_DELIVERY, DeliveryStatus.PROVISIONAL})
        now = time.time()
        async with client_ctx(RedisClient, self.settings.redis) as r:
            for message_id in await self._indexed_ids(r, wanted, now):
                hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
                if not hashed:
                    await self._drop_orphan(r, message_id)
                    continue
                try:
                    # Parse the WHOLE row (content blob included), as list_by_status does: a
                    # row malformed anywhere is skipped here, not handed to a delivery that
                    # re-reads it unguarded and re-drives forever.
                    self._from_hash(hashed)
                    status = DeliveryStatus(hashed[_F_STATUS])
                    if status not in wanted:
                        # Moved on between the index read and this one; its new state is
                        # whoever wrote it to answer for.
                        continue
                    grace = hashed.get(_F_GRACE) or ""
                    found = PendingWork(
                        message_id=message_id,
                        delivery_status=status,
                        attempts=int(hashed[_F_ATTEMPTS]),
                        grace_deadline=float(grace) if grace else None,
                    )
                except (ValueError, KeyError):
                    # One unreadable row must not abort the pass, or every other record
                    # with unfinished work stays stranded behind it forever.
                    logger.warning(
                        "conversations: record %r is corrupt and was skipped in the delivery sweep", message_id
                    )
                    continue
                work.append(found)
        return work

    async def list_by_status(self, statuses: frozenset[DeliveryStatus]) -> list[ConversationRecord]:
        """Every record whose ``delivery_status`` is in ``statuses``, read from the status
        index. An unparseable row is logged and skipped rather than crashing the listing."""
        records: list[ConversationRecord] = []
        wanted = {status.value for status in statuses}
        now = time.time()
        async with client_ctx(RedisClient, self.settings.redis) as r:
            for message_id in await self._indexed_ids(r, statuses, now):
                hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
                if not hashed:
                    await self._drop_orphan(r, message_id)
                    continue
                if hashed.get(_F_STATUS) not in wanted:
                    continue
                try:
                    records.append(self._from_hash(hashed))
                except (ValueError, KeyError):
                    logger.warning(
                        "conversations: record %r is corrupt and was skipped in the status listing", message_id
                    )
        return records

    # -- thread indexes (keyspaces 7-8) --------------------------------------

    async def list_route_threads(self, route_name: str, *, offset: int, limit: int) -> ThreadPage:
        """One page of ``route_name``'s threads, newest activity first, each summarized from
        its NEWEST readable record — so the cost is the page, never the route's whole
        history. Reads nothing back into the index: a thread with no readable record left is
        logged and omitted from the page, and the prune pass reclaims it."""
        key = self.settings.route_threads_key(route_name)
        threads: list[ThreadSummary] = []
        async with client_ctx(RedisClient, self.settings.redis) as r:
            # Read the total the page is cut from BEFORE the page, so ``next_page`` can
            # never be computed against a count the walk itself moved.
            total = int(await awaited(r.zcard(key)))
            for member, score in await awaited(r.zrevrange(key, offset, offset + limit - 1, withscores=True)):
                summary = await self._thread_summary(r, route_name, _member(member), last_activity_at=float(score))
                if summary is not None:
                    threads.append(summary)
        return ThreadPage(threads=threads, total=total)

    async def list_thread_records(
        self, route_name: str, thread_id: str, *, offset: int, limit: int, newest_first: bool = False
    ) -> TranscriptPage:
        """One page of a thread's records — oldest first by default, newest first with
        ``newest_first`` (the live-tail order, where page 1 always holds the latest
        messages). Either way the window is ``offset``/``limit`` ranks from that end of the
        index, which is scored by ``created_at``.

        ``total`` is 0 exactly when the index holds no record for that thread, which is how
        an unknown or fully expired thread is told from an empty page. A member whose row is
        gone or unparseable is logged and skipped; the index is left alone, so the page's
        offsets and ``total`` stay the ones the caller asked against."""
        thread_key = self.settings.thread_index_key(route_name, thread_id)
        records: list[ConversationRecord] = []
        async with client_ctx(RedisClient, self.settings.redis) as r:
            total = int(await awaited(r.zcard(thread_key)))
            if total == 0:
                return TranscriptPage(records=[], total=0)
            end = offset + limit - 1
            window = r.zrevrange(thread_key, offset, end) if newest_first else r.zrange(thread_key, offset, end)
            for member in await awaited(window):
                message_id = _member(member)
                hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
                if not hashed:
                    logger.warning(
                        "conversations: record %r is indexed in thread %r but has no row; "
                        "skipped in the transcript, left for the prune pass",
                        message_id,
                        thread_id,
                    )
                    continue
                try:
                    records.append(self._from_hash(hashed))
                except (ValueError, KeyError):
                    logger.warning(
                        "conversations: record %r is corrupt and was skipped in the thread transcript", message_id
                    )
        return TranscriptPage(records=records, total=total)

    async def list_person_thread_records(
        self, route_names: list[str], thread_id: str, *, offset: int, limit: int, newest_first: bool = False
    ) -> TranscriptPage:
        """One page of a LINKED person's aggregated transcript — the k-way merge of the same
        ``thread_id`` (``bridge:@person:{id}``) across the person's N per-route indexes, so
        one full history is served and never a partial slice.

        ``total`` is ``Σ zcard`` over the N indexes. For a page at ``offset``/``limit`` the
        first ``offset + limit`` members are fetched from EACH index in the requested order
        (``zrange`` ascending, ``zrevrange`` descending — the front of the page can draw at
        most that many from any one index), merged and sorted by ``(created_at, message_id)``
        in that same direction — the ``message_id`` tie-break mirrors redis's own equal-score
        member ordering, so cross-index ties page deterministically — then sliced
        ``[offset : offset + limit]``. NEVER a per-index offset (wrong global pages) and never
        an ascending fetch reversed (wrong descending window)."""
        thread_keys = [self.settings.thread_index_key(route_name, thread_id) for route_name in route_names]
        end = offset + limit - 1
        scored: list[tuple[float, str]] = []
        async with client_ctx(RedisClient, self.settings.redis) as r:
            total = 0
            for key in thread_keys:
                total += int(await awaited(r.zcard(key)))
            if total == 0:
                return TranscriptPage(records=[], total=0)
            for key in thread_keys:
                window = (
                    r.zrevrange(key, 0, end, withscores=True)
                    if newest_first
                    else r.zrange(key, 0, end, withscores=True)
                )
                scored.extend((float(score), _member(member)) for member, score in await awaited(window))
            scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=newest_first)
            records: list[ConversationRecord] = []
            for _score, message_id in scored[offset : offset + limit]:
                hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
                if not hashed:
                    logger.warning(
                        "conversations: record %r is indexed in person thread %r but has no row; "
                        "skipped in the transcript, left for the prune pass",
                        message_id,
                        thread_id,
                    )
                    continue
                try:
                    records.append(self._from_hash(hashed))
                except (ValueError, KeyError):
                    logger.warning(
                        "conversations: record %r is corrupt and was skipped in the person transcript", message_id
                    )
        return TranscriptPage(records=records, total=total)

    async def _thread_summary(
        self, r: AsyncRedis, route_name: str, thread_id: str, *, last_activity_at: float
    ) -> ThreadSummary | None:
        """``thread_id`` summarized from the newest record readable among its
        :data:`_THREAD_SUMMARY_SCAN` newest members, or ``None`` when none of them is.

        ``last_activity_at`` is the route index's own score — the value the listing SORTS
        by — passed in so the row shown and the order it is shown in can never disagree.
        Reads nothing back into the index."""
        thread_key = self.settings.thread_index_key(route_name, thread_id)
        for member in await awaited(r.zrevrange(thread_key, 0, _THREAD_SUMMARY_SCAN - 1)):
            message_id = _member(member)
            hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
            if not hashed:
                continue
            try:
                record = self._from_hash(hashed)
            except (ValueError, KeyError):
                # Keep scanning: one unreadable record must not hide a live thread from the
                # operator listing forever — its OTHER records are readable, and the prune
                # never reclaims a thread whose rows exist.
                logger.warning(
                    "conversations: record %r is corrupt and was skipped in thread %r's summary scan",
                    message_id,
                    thread_id,
                )
                continue
            return ThreadSummary(
                thread_id=thread_id,
                client_address=record.client_address,
                last_activity_at=last_activity_at,
                message_count=int(await awaited(r.zcard(thread_key))),
                last_delivery_status=record.delivery_status,
            )
        logger.warning(
            "conversations: thread %r on route %r has no readable record among its %d newest members; "
            "omitted from the listing, left for the prune pass",
            thread_id,
            route_name,
            _THREAD_SUMMARY_SCAN,
        )
        return None

    async def prune_expired_terminal_indexes(
        self, route_names: Iterable[str], cursor: PruneCursor = PRUNE_START
    ) -> PruneCursor:
        """Drop the index members no read reaches, so neither index outgrows the retained
        keyspace it names. The ONLY place either thread index shrinks outside a record
        delete — the reads deliberately leave it alone.

        Terminal-status members are dropped by score: a member's score is its row's exact
        expiry moment, and the ``delivered``/``shed``/``silent`` indexes are read by nothing
        (the live and on-demand ones are pruned lazily on every read).

        Every thread of every named route is then offered its expired members — a member
        scored older than the retention window whose row is indeed gone — so a long-lived
        ACTIVE thread stops over-reporting its ``message_count`` too, not just an idle one.

        One pass is BOUNDED in every dimension: it reads at most
        :data:`_PRUNE_THREADS_PER_BATCH` threads and :data:`_PRUNE_MEMBERS_PER_BATCH`
        members per command, and spends at most :data:`_PRUNE_WORK_PER_PASS` units of work,
        one per thread EXAMINED plus one per candidate member offered. A route holding a
        hundred thousand healthy threads therefore costs a fixed pass, not a hundred
        thousand round trips.

        Takes the cursor the previous pass returned and returns where this one stopped, so
        the work left over is picked up from there rather than re-read from the front (see
        :class:`PruneCursor`). A pass that walked every route returns
        :data:`PRUNE_START`."""
        now = time.time()
        expired_before = now - self.settings.answer_retention_ttl_seconds
        budget = _PRUNE_WORK_PER_PASS
        routes = _resume_at(list(route_names), cursor.route_name)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            for status in TERMINAL_STATUSES:
                await awaited(r.zremrangebyscore(self.settings.status_index_key(status.value), "-inf", now))
            for position, route_name in enumerate(routes):
                resuming = position == 0 and route_name == cursor.route_name
                spent, stopped = await self._prune_route(
                    r,
                    route_name,
                    expired_before,
                    thread_rank=cursor.thread_rank if resuming else 0,
                    member_rank=cursor.member_rank if resuming else 0,
                    member_thread=cursor.thread_id if resuming else None,
                    budget=budget,
                )
                budget -= spent
                if stopped is not None:
                    return stopped
                if budget <= 0:
                    following = routes[position + 1 :]
                    return PruneCursor(following[0]) if following else PRUNE_START
        return PRUNE_START

    async def _prune_route(
        self,
        r: AsyncRedis,
        route_name: str,
        expired_before: float,
        *,
        thread_rank: int,
        member_rank: int,
        member_thread: str | None,
        budget: int,
    ) -> tuple[int, PruneCursor | None]:
        """Walk one route's threads from ``thread_rank`` on, in rank windows of
        :data:`_PRUNE_THREADS_PER_BATCH`, spending at most ``budget`` units of work.

        Returns ``(spent, stopped)``, where ``stopped`` is the cursor to resume from when
        the budget ran out mid-route and ``None`` when the route was walked to its end. A
        thread that empties leaves the route index and shifts the threads behind it down one
        rank, so the walking rank advances only over the threads that SURVIVED — the next
        window can then neither skip a thread nor re-read one.

        ``member_rank`` is applied only to ``member_thread``: between two passes a thread at
        a LOWER rank can vanish and shift this one out from under the rank the last pass
        recorded, and a member offset carried into a different thread would silently skip
        that thread's first members."""
        key = self.settings.route_threads_key(route_name)
        spent = 0
        rank = thread_rank
        resume_member = member_rank
        resume_thread = member_thread
        while spent < budget:
            members = await awaited(r.zrange(key, rank, rank + _PRUNE_THREADS_PER_BATCH - 1))
            if not members:
                return spent, None
            for member in members:
                thread_id = _member(member)
                if thread_id != resume_thread:
                    # Not the thread the member offset was measured in: start it from rank 0.
                    resume_member, resume_thread = 0, thread_id
                if spent >= budget:
                    return spent, PruneCursor(route_name, rank, resume_member, resume_thread)
                step = await self._prune_thread(
                    r,
                    route_name,
                    thread_id,
                    expired_before,
                    start_rank=resume_member,
                    budget=budget - spent - 1,
                )
                # One unit for EXAMINING the thread, so a route of healthy threads — each
                # costing one ranged read and buying no member work — is bounded too.
                spent += 1 + step.spent
                resume_member, resume_thread = 0, None
                if step.resume_rank is not None:
                    return spent, PruneCursor(route_name, rank, step.resume_rank, thread_id)
                if not step.emptied:
                    rank += 1
        return spent, PruneCursor(route_name, rank, 0)

    async def _prune_thread(
        self,
        r: AsyncRedis,
        route_name: str,
        thread_id: str,
        expired_before: float,
        *,
        start_rank: int,
        budget: int,
    ) -> _ThreadPruneStep:
        """Offer one thread's retention-expired candidates to the atomic prune step, from
        ``start_rank`` on, one bounded batch at a time until the thread has none left or
        ``budget`` units of work are spent.

        A candidate whose row is still there is NOT removed, so the rank of the next
        unexamined member is the rank walked to minus the members the step did remove.

        The first batch always runs, so a thread reached with no budget left still costs one
        ranged read: the pass may overshoot by one batch per thread rather than charge for
        work it did not do.

        A thread with NO candidate is still offered to the step, so one whose index has run
        empty leaves the route index here. Nothing else would ever drop it: the reads
        deliberately leave the index alone, and a thread that can offer no member would
        otherwise be examined and skipped by every pass forever, over-counting the route's
        ``total``, re-logging the read doors' orphan warning and blocking a door edit behind
        a count with no visible thread."""
        thread_key = self.settings.thread_index_key(route_name, thread_id)
        route_key = self.settings.route_threads_key(route_name)
        spent = 0
        rank = start_rank
        while True:
            candidates = [
                _member(member)
                for member in await awaited(
                    r.zrangebyscore(thread_key, "-inf", expired_before, start=rank, num=_PRUNE_MEMBERS_PER_BATCH)
                )
            ]
            if not candidates:
                _, remaining = await eval_script(r, _PRUNE_THREAD_LUA, 2, thread_key, route_key, thread_id)
                return _ThreadPruneStep(spent, None, emptied=int(remaining) == 0)
            keys = [thread_key, route_key]
            keys.extend(self.settings.record_key(message_id) for message_id in candidates)
            removed, remaining = await eval_script(r, _PRUNE_THREAD_LUA, len(keys), *keys, thread_id, *candidates)
            spent += len(candidates)
            rank += len(candidates) - int(removed)
            if int(remaining) == 0:
                return _ThreadPruneStep(spent, None, emptied=True)
            if len(candidates) < _PRUNE_MEMBERS_PER_BATCH:
                return _ThreadPruneStep(spent, None, emptied=False)
            if spent >= budget:
                return _ThreadPruneStep(spent, rank, emptied=False)

    async def count_route_threads(self, route_name: str) -> int:
        """How many threads ``route_name``'s index holds — the count the delete door reads
        to tell an unknown route from one whose reclamation was interrupted and is owed."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return int(await awaited(r.zcard(self.settings.route_threads_key(route_name))))

    async def drop_route_threads(self, route_name: str) -> None:
        """Delete a route's thread indexes — every per-thread transcript ZSET the route's
        thread index names, then the index itself. Neither carries a TTL and the prune pass
        only walks LIVE routes, so a deleted route's indexes are unreachable unless its
        delete reclaims them here. The records themselves are left to their own retention
        TTL.

        Walked in rank windows of :data:`_PRUNE_THREADS_PER_BATCH`, so no single reply
        carries a whole route's threads; each window leaves the route index as it is
        handled, which is what makes re-reading rank 0 walk forward, and what makes an
        interrupted run RESUMABLE: a thread's own index is deleted before the thread leaves
        the route index, so the route index still names every thread a stopped run had not
        finished with, and re-running this reclaims exactly the remainder. That index is
        therefore the durable marker a repeated delete finds the work by — which is why the
        delete door treats a name with a surviving route index as reclaimable rather than
        as an unknown route."""
        key = self.settings.route_threads_key(route_name)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            while True:
                members = [_member(member) for member in await awaited(r.zrange(key, 0, _PRUNE_THREADS_PER_BATCH - 1))]
                if not members:
                    break
                for thread_id in members:
                    await awaited(r.delete(self.settings.thread_index_key(route_name, thread_id)))
                await awaited(r.zrem(key, *members))
            await awaited(r.delete(key))

    async def thread_has_live_intake(self, thread_id: str) -> bool:
        """Whether a turn is IN FLIGHT on ``thread_id`` — an ``accepted`` intake record still
        holding a LIVE lease. That turn's completion re-stamps the thread indexes and writes
        checkpoint state, so a delete racing it would half-forget the memory. The intake lease
        is the CROSS-WORKER liveness marker the re-drive trusts (an ``accepted`` record whose
        lease expiry is still ahead of now); the per-worker FIFO reservation cannot answer for
        a turn running on a sibling worker, so the lease is read here, never taken. Scanned
        off the ``accepted`` status index, which holds only the turns in flight fleet-wide."""
        now = time.time()
        accepted_key = self.settings.status_index_key(DeliveryStatus.ACCEPTED.value)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            for member in await awaited(r.zrange(accepted_key, 0, -1)):
                message_id = _member(member)
                hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
                if hashed.get(_F_STATUS) != DeliveryStatus.ACCEPTED.value:
                    continue
                try:
                    record_thread_id = json.loads(hashed[_F_DATA])["thread_id"]
                except (ValueError, KeyError):
                    logger.warning(
                        "conversations: record %r is corrupt and was skipped in the in-flight check", message_id
                    )
                    continue
                if record_thread_id != thread_id:
                    continue
                _token, sep, expiry = (hashed.get(_F_INTAKE) or "").partition(":")
                if sep and expiry and float(expiry) > now:
                    return True
        return False

    async def drop_thread(self, route_name: str, thread_id: str) -> int:
        """Delete one thread under ``route_name`` outright — every answer record its transcript
        index names, that index, and the thread's membership in the route index. Returns the
        number of record ROWS removed (an index member whose row already expired counts 0 yet
        is still unindexed).

        RETRYABLE: draining the transcript index reclaims the route member with the last
        record (the same atomic step a record delete takes), so an interrupted run leaves the
        still-indexed remainder for a re-run, and the trailing deletes cover an index already
        emptied and a route member stranded without one. Neither thread index carries a TTL and
        the prune pass walks LIVE routes only, so nothing here may leave a member behind: the
        operator asked for the thread gone now, not on the records' own retention clock."""
        thread_key = self.settings.thread_index_key(route_name, thread_id)
        route_key = self.settings.route_threads_key(route_name)
        status_keys = [self.settings.status_index_key(status.value) for status in _INDEXED_STATUSES]
        removed = 0
        async with client_ctx(RedisClient, self.settings.redis) as r:
            while True:
                members = [
                    _member(member) for member in await awaited(r.zrange(thread_key, 0, _PRUNE_MEMBERS_PER_BATCH - 1))
                ]
                if not members:
                    break
                for message_id in members:
                    keys = [self.settings.record_key(message_id), *status_keys, thread_key, route_key]
                    removed += int(await eval_script(r, _DELETE_LUA, len(keys), *keys, message_id, thread_id))
            await awaited(r.delete(thread_key))
            await awaited(r.zrem(route_key, thread_id))
        return removed

    # -- outbound reverse index (keyspace 3) ---------------------------------

    async def index_outbound(self, channel: str, outbound_ids: list[str], message_id: str) -> None:
        """Map each outbound provider id back to ``message_id`` so an out-of-band receipt
        resolves to the record. Written with the retention TTL, so it is swept with it."""
        if not outbound_ids:
            return
        ttl = self.settings.answer_retention_ttl_seconds
        async with client_ctx(RedisClient, self.settings.redis) as r:
            for outbound_id in outbound_ids:
                await awaited(r.set(self.settings.outbound_index_key(channel, outbound_id), message_id, ex=ttl))

    async def resolve_outbound(self, channel: str, outbound_id: str) -> str | None:
        """The ``message_id`` an outbound provider id maps to, or ``None`` when the index
        holds no such id (an unknown id, or one already swept)."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            raw = await awaited(r.get(self.settings.outbound_index_key(channel, outbound_id)))
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw


__all__ = [
    "PRUNE_START",
    "ConversationRecordStore",
    "PendingWork",
    "PruneCursor",
    "ThreadPage",
    "ThreadSummary",
    "TranscriptPage",
]
