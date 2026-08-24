"""Redis store for the interactions capability.

Holds every key shape and the read/write operations behind one class so the
producer (the ``ask_user`` helper in this package) and the consumer (the API SSE
+ answer endpoint) share the exact key contract. Operations take the redis
client as an argument: each caller opens it from the interactions settings via
``client_ctx(RedisClient, settings.redis)``.

Requires Redis server >= 7.0: ``add`` sets-or-extends the group stream and
``count_key`` TTLs with ``EXPIRE ... NX`` + ``EXPIRE ... GT`` (Redis 7.0+) and
extends the pending-deadline index with ``ZADD ... GT`` (Redis 6.2+). Against an
older server these commands error loudly (a visible break, never a silent degrade).

Assumes a single-node Redis (not Redis Cluster): the phantom-purge Lua drops
per-group index members read at runtime rather than from ``KEYS`` (the number of
expired groups is variable), which a Cluster would reject as an undeclared-key
access. The interactions keys share one prefix and are not hash-tag co-located,
so single-node is the operating assumption.

Loud by contract — no swallowed errors, no silent fallback.
"""

from __future__ import annotations

import asyncio
import enum
import json
import math
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast, overload

from redis.asyncio import Redis
from redis.exceptions import WatchError
from tai42_contract.interactions import (
    MEDIA_ROUTE_PREFIX,
    InteractionRequest,
    InteractionResponse,
    InteractionState,
)

ADD_EVENT = "interaction.add"
ANSWERED_EVENT = "interaction.answered"
REMOVED_EVENT = "interaction.removed"
_EVENTS_MAXLEN = 10000


def _event_fields(event_type: str, interaction_id: str, group_id: str, audience: str | None) -> dict[str, str]:
    # An answered/removed event frame. ``audience`` rides it so the tail-only SSE
    # filters the frame directly (a restricted caller sees only its own); it is
    # omitted when None (an unaddressed question) — a redis stream field is never None.
    fields = {"type": event_type, "interaction_id": interaction_id, "group_id": group_id}
    if audience is not None:
        fields["audience"] = audience
    return fields


# The three end states ``prune_pending`` distinguishes: ``"pruned"`` deleted a
# still-pending question; ``"answered"`` found an answered status (no writes);
# ``"gone"`` found no state key at all (missing/expired, no writes).
PruneResult = Literal["pruned", "answered", "gone"]

# Atomic phantom self-heal for the pending-deadline index. A waiter killed
# mid-flight (SIGKILL/OOM) never runs cleanup, so its group lingers in
# ``pending_key``. This script — run on every ``add`` — reads the groups whose
# furthest question deadline has passed and, per expired group, drops it from BOTH
# the pending index and the parallel deadline index. It leaves ``count_key``
# untouched: the count is set-or-extended to the group's TTL (see ``add``), so a
# surviving state always keeps a live count and a genuinely-dead group's count
# expires on that same basis — death and revival stay symmetric, and a group that
# revives after a purge cannot re-seed a torn count. Reading the expired set
# INSIDE the script makes the correlated multi-index delete atomic: a concurrent
# ``add`` that revives a group (later deadline via ``ZADD GT``, re-added to
# ``pending_key``) between a would-be read and delete cannot be wrongly purged,
# because the script re-reads the current deadline index rather than acting on a
# stale snapshot.
#
# The group of the ``add`` running this purge is SKIPPED: this call is about to
# make that group live (its future deadline is not recorded via ``ZADD GT`` until
# the pipeline that follows the purge), so scanning it as "expired" and purging it
# would drop a group that is gaining a live question — invariant (b). The
# phantom self-heal of a genuinely dead group still fires, driven by any UNRELATED
# ``add``.
#   KEYS[1] = pending_deadline_key,  KEYS[2] = pending_key
#   ARGV[1] = now_ms (purge cutoff),  ARGV[2] = group to skip (the current add's group)
_PENDING_PURGE_LUA = """
-- interactions:pending-deadline-purge
local current = ARGV[2]
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], 0, ARGV[1])
local purged = 0
for _, group in ipairs(expired) do
    if group ~= current then
        redis.call('ZREM', KEYS[2], group)
        redis.call('ZREM', KEYS[1], group)
        purged = purged + 1
    end
end
return purged
"""

# Atomic reserve-and-check for the ``max_concurrent`` cap. The open index carries
# no TTL, so a SIGKILLed waiter's member lingers; this script first purges every
# member whose deadline has passed, then admits the caller — adding its open-index
# member — ONLY while the live count is below ``limit``. ZCARD and the ZADD run in
# one server round trip, so a concurrent burst can never overshoot the cap the way
# a separate count-then-add pair can (the check-then-act gap between two commands).
#   KEYS[1] = open_key
#   ARGV[1] = now_ms (stale-member cutoff),  ARGV[2] = limit,
#   ARGV[3] = timeout_at_ms (member score),  ARGV[4] = interaction_id (member)
_OPEN_RESERVE_LUA = """
-- interactions:open-slot-reserve
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
if tonumber(redis.call('ZCARD', KEYS[1])) >= tonumber(ARGV[2]) then
    return 0
end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
return 1
"""


# Set-or-extend the group media index and every member media key to the group's TTL,
# queued into the add's write pipeline so it commits atomically with the question. Reads
# the current index members, SADDs the add's new ids, then — over the union — sets the
# index and each ``media:{id}`` key to the horizon via
# ``EXPIRE ... NX`` (set when a key has none yet) + ``EXPIRE ... GT`` (raise only when
# longer), the same SET-OR-EXTEND-TO-GREATER discipline the group stream and count use.
# So a co-grouped long park keeps the whole group's media alive and a later short add
# never shrinks it. Like the phantom purge, it constructs the per-member ``media:{id}``
# keys from members read at runtime — a single-node access (undeclared for Cluster),
# consistent with this store's single-node assumption.
#   KEYS[1] = media_index_key
#   ARGV[1] = ttl seconds,  ARGV[2] = media_key prefix,  ARGV[3..] = the add's new ids
_MEDIA_SET_OR_EXTEND_LUA = """
-- interactions:media-set-or-extend
local ttl = tonumber(ARGV[1])
local prefix = ARGV[2]
local members = redis.call('SMEMBERS', KEYS[1])
local seen = {}
for _, id in ipairs(members) do seen[id] = true end
for i = 3, #ARGV do
    local id = ARGV[i]
    if not seen[id] then
        redis.call('SADD', KEYS[1], id)
        members[#members + 1] = id
        seen[id] = true
    end
end
if #members > 0 then
    redis.call('EXPIRE', KEYS[1], ttl, 'NX')
    redis.call('EXPIRE', KEYS[1], ttl, 'GT')
    for _, id in ipairs(members) do
        local mk = prefix .. id
        redis.call('EXPIRE', mk, ttl, 'NX')
        redis.call('EXPIRE', mk, ttl, 'GT')
    end
end
return #members
"""


# Atomic retry-claim for a durable continuation-due record. The reaper's
# redelivery pass runs this per due member: it advances the record's next-attempt
# score by an exponential backoff BEFORE returning the record to fire, so a
# concurrent reaper pass reading the SAME member finds the score already pushed
# past ``now`` and declines (returns nil) — one pass re-fires per backoff window.
# At-least-once, not exactly-once: the fired continuation's consumer must be
# idempotent (an at-least-once redelivery may fire the same continuation more than
# once), so even a rare double-fire across passes is harmless. A member whose record
# hash has vanished (TTL-expired past the retention horizon) is an orphan index
# entry — reconciled off the index (ZREM) rather than re-firing a record that no
# longer exists, and returns the ``'dropped'`` marker so the caller can surface a
# LOUD terminal give-up (no further redelivery will ever fire that resume).
#   KEYS[1] = continuation_due_index_key,  KEYS[2] = continuation_due_record_key
#   ARGV[1] = interaction_id (member),  ARGV[2] = now_ms (due cutoff),
#   ARGV[3] = backoff_base_ms,  ARGV[4] = backoff_cap_ms
_CONTINUATION_RETRY_CLAIM_LUA = """
-- interactions:continuation-retry-claim
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score then return nil end
if tonumber(score) > tonumber(ARGV[2]) then return nil end
if redis.call('EXISTS', KEYS[2]) == 0 then
    redis.call('ZREM', KEYS[1], ARGV[1])
    return 'dropped'
end
local attempts = redis.call('HINCRBY', KEYS[2], 'attempts', 1)
local delay = tonumber(ARGV[3]) * (2 ^ (attempts - 1))
if delay > tonumber(ARGV[4]) then delay = tonumber(ARGV[4]) end
redis.call('ZADD', KEYS[1], tonumber(ARGV[2]) + delay, ARGV[1])
return redis.call('HGETALL', KEYS[2])
"""


@dataclass(frozen=True)
class ContinuationDue:
    """A durable continuation-due record read back for redelivery — self-contained
    and FLOW-BLIND: a registered tool NAME, the generic ``{interaction_id, answer}``
    the continuation runs with, and the stored execution identity + key fingerprint.
    Carries nothing engine/flow/session specific."""

    interaction_id: str
    tool: str
    identity: str
    fingerprint: str
    answer: Any
    attempts: int


class ContinuationRetryDrop(enum.Enum):
    """The sole non-record outcome of ``claim_continuation_retry``: the due member's
    record TTL-expired past its retention horizon and the orphan index member was
    reconciled off — a permanent, terminal drop no further redelivery can ever fire.
    Distinct from ``None`` (not yet / no longer due — a benign no-op)."""

    DROPPED = "dropped"


# The reaper surfaces a loud terminal give-up when the claim returns this.
CONTINUATION_DROPPED: Final = ContinuationRetryDrop.DROPPED


def _continuation_due_mapping(tool: str, identity: str, fingerprint: str, answer: Any) -> dict[str, str]:
    """The flow-blind continuation-due record fields: a registered tool NAME, the
    stored execution identity + key fingerprint, the generic answer (JSON-encoded so
    any answer shape — a scalar, the expiry sentinel, a form object — round-trips), and
    a zeroed attempt count. Nothing engine/flow/session specific. Written into the SAME
    MULTI as the resolving claim (``record_answer``), so the outbox enqueue commits
    atomically with the ``answered`` state change — a crash can never leave a claimed
    answer with no due-record."""
    return {
        "tool": tool,
        "identity": identity,
        "fingerprint": fingerprint,
        "answer": json.dumps(answer),
        "attempts": "0",
    }


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def _created_ms(request: InteractionRequest) -> int:
    return int(request.created_at.timestamp() * 1000)


def _timeout_ms(request: InteractionRequest) -> int:
    return int(request.timeout_at.timestamp() * 1000)


def _expiry_ms(request: InteractionRequest) -> int:
    # Only called for an async request whose ``expiry_at`` is set (guarded at the
    # call site); a None here is a caller bug, never a silent 0.
    if request.expiry_at is None:
        raise ValueError("_expiry_ms called on a request with no expiry_at")
    return int(request.expiry_at.timestamp() * 1000)


# Fallback ``expiry_ttl_margin_seconds`` for a direct ``add`` caller (the helper
# always passes a reaper-derived value); a park with an ``expiry_at`` within
# ``idle_ttl`` never reaches this since its TTL floors to ``idle_ttl``.
_DEFAULT_EXPIRY_TTL_MARGIN_SECONDS = 60


def _media_ids_of(request: InteractionRequest) -> list[str]:
    # The stored-media ids a request's media references — items whose url is a served
    # reference (``MEDIA_ROUTE_PREFIX{id}``); https/link/data: items carry none. A
    # data:image is substituted to a served reference before the request is built, so
    # by the time it reaches ``add`` only served references remain to index.
    if not request.media:
        return []
    return [item.url[len(MEDIA_ROUTE_PREFIX) :] for item in request.media if item.url.startswith(MEDIA_ROUTE_PREFIX)]


def _key_ttl(request: InteractionRequest, idle_ttl: int, now_ms: int, expiry_margin_s: int) -> int:
    """The TTL a question's own keys need. An async park with an ``expiry_at`` beyond
    the idle horizon must survive to its expiry PLUS a reaper-pass margin, or its
    state hash would expire before the reaper reads it — leaving the question
    unanswerable in the ``idle_ttl``..``expiry_at`` gap and stranding the
    continuation. Every other question (sync, or a park expiring within the idle
    horizon) uses the flat ``idle_ttl``."""
    if request.mode == "async" and request.expiry_at is not None:
        horizon = math.ceil((_expiry_ms(request) - now_ms) / 1000) + expiry_margin_s
        return max(idle_ttl, horizon)
    return idle_ttl


@overload
def as_str(value: None) -> None: ...
@overload
def as_str(value: str | bytes | bytearray) -> str: ...
def as_str(value: str | bytes | bytearray | None) -> str | None:
    """Normalize a redis value to ``str`` whether the client decodes or not."""
    if value is None:
        return None
    return value.decode() if isinstance(value, (bytes, bytearray)) else value


class InteractionStore:
    def __init__(self, key_prefix: str) -> None:
        self._p = key_prefix

    # -- key shapes ----------------------------------------------------------

    def group_key(self, group_id: str) -> str:
        return f"{self._p}group:{group_id}"

    def state_key(self, interaction_id: str) -> str:
        return f"{self._p}state:{interaction_id}"

    def reply_key(self, interaction_id: str) -> str:
        return f"{self._p}reply:{interaction_id}"

    def ticket_key(self, ticket: str) -> str:
        return f"{self._p}ticket:{ticket}"

    @property
    def pending_key(self) -> str:
        return f"{self._p}pending"

    @property
    def pending_deadline_key(self) -> str:
        """Parallel index to ``pending_key``, scored by each group's FURTHEST
        question deadline (extend-only via ``ZADD GT``). ``pending_key`` is scored
        by creation TIME, not deadline — ``add`` sets a group's score to its most
        recent question's ``created_at`` — so the pending list reads in
        creation-timestamp order rather than deadline order; only this parallel
        index carries the deadline the atomic phantom purge keys on, and the purge
        never rescores ``pending_key``."""
        return f"{self._p}pending:deadline"

    @property
    def open_key(self) -> str:
        return f"{self._p}open"

    @property
    def pending_expiry_key(self) -> str:
        """Per-INTERACTION deadline index for async parks: member = interaction id,
        score = ``expiry_at`` in ms. Distinct from ``pending_deadline_key`` (group
        keyed, scored by sync ``timeout_at``, purged of expired GROUPS on every
        ``add``): an async park's continuation must fire exactly once at expiry, so
        it needs an interaction-level index the group phantom-purge never drops.
        The expiry reaper scans it; ``record_answer``/``prune_pending`` remove a
        member when the question leaves pending. Every async park carries an
        ``expiry_at`` and so is a member; a sync question has no expiry and is never
        indexed here."""
        return f"{self._p}pending:expiry"

    def continuation_due_key(self, interaction_id: str) -> str:
        """The durable continuation-due record for one async park: a self-contained,
        flow-blind hash (tool NAME, execution identity, key fingerprint, generic
        answer, attempt count) written when the park resolves and cleared when its
        continuation's ``run_tool`` returns. Its presence past a healthy fire's
        window is the reaper's signal to redeliver."""
        return f"{self._p}continuation:due:{interaction_id}"

    @property
    def continuation_due_index_key(self) -> str:
        """Deadline index over the continuation-due records: member = interaction id,
        score = the next-attempt time in ms. The redelivery reaper scans it for
        members due at or before now; a member is dropped when its continuation's
        ``run_tool`` returns (``clear_continuation_due``) or reconciled off when its
        record hash has TTL-expired."""
        return f"{self._p}continuation:due"

    @property
    def _count_prefix(self) -> str:
        return f"{self._p}pending:count:"

    def count_key(self, group_id: str) -> str:
        return f"{self._count_prefix}{group_id}"

    @property
    def events_key(self) -> str:
        return f"{self._p}events"

    def media_key(self, media_id: str) -> str:
        """The hash holding one stored-by-reference media item (its mime + base64
        bytes). Keyed by the media id (the served-media capability secret); its TTL
        is set-or-extended to the owning group's horizon on every ``add`` (media of a
        group lives as long as the group)."""
        return f"{self._p}media:{media_id}"

    def media_index_key(self, group_id: str) -> str:
        """SET of the stored-media ids a group's questions reference. ``add`` extends
        every member ``media_key`` and this index to the group's TTL, so the bytes a
        durable record points at outlive no shorter than the group stream."""
        return f"{self._p}media-index:{group_id}"

    def thread_parks_key(self, thread_id: str) -> str:
        """SET of the interaction ids of the async PARKS bound to one conversation
        thread — the reverse index a thread delete reads to cascade-cancel every
        parked ``ask_user`` the deletion would otherwise ORPHAN (its expiry reaper
        later firing a continuation into a thread that no longer exists, its channel
        correlation muted until the deadline). There is otherwise no thread→interaction
        edge: the state is keyed by interaction id alone.

        A member is written in the park's OWN ``add`` pipeline — only for an async park
        that carries a bound thread (a background tool run with no thread writes none) —
        so it commits atomically with the ``state``/``pending``/expiry writes. It is
        dropped wherever the interaction leaves pending (``record_answer`` and
        ``prune_pending``), keyed off the ``thread_id`` denormalized on the state hash.
        The set's TTL is set-or-extended to the park's own ``_key_ttl`` (the same NX+GT
        discipline the group stream uses), so it outlives the longest-lived park it
        indexes; drained empty it simply expires."""
        return f"{self._p}thread-parks:{thread_id}"

    # -- writes --------------------------------------------------------------

    async def add(
        self,
        r: Redis,
        request: InteractionRequest,
        idle_ttl: int,
        ticket: str | None = None,
        ticket_ttl: int | None = None,
        open_member_reserved: bool = False,
        continuation_fingerprint: str | None = None,
        expiry_ttl_margin_seconds: int = _DEFAULT_EXPIRY_TTL_MARGIN_SECONDS,
        thread_id: str | None = None,
    ) -> None:
        """Persist a new question: stream entry, state, pending index + deadline
        index + count, the open-index ZSET member, add-event, and refreshed TTLs.
        The TTL refresh gives this question's own state hash its own ``_key_ttl`` — it
        SELF-COVERS, relying on no sibling to refresh it — and extends the shared group
        stream and ``count_key`` to the greatest horizon across the group's parks, so a
        still-open question always has a live group stream and count.

        A key's TTL is its ``_key_ttl``: an async park whose ``expiry_at`` runs past
        the ``idle_ttl`` horizon gets a TTL covering that expiry plus
        ``expiry_ttl_margin_seconds`` (a reaper-pass margin the helper derives from
        the reaper interval), so its state survives to be answered and reaped rather
        than expiring mid-park. This question's own state hash takes its own
        ``_key_ttl``; the shared group stream and ``count_key`` take a
        SET-OR-EXTEND-TO-GREATER TTL (``EXPIRE ... NX`` to set the first horizon,
        ``EXPIRE ... GT`` to raise it only when longer), so concurrent adds to the
        same group converge both keys to the MAX horizon across all the group's
        parks regardless of commit order and neither can be shrunk below a
        co-grouped long park's horizon by a later short-horizon add.

        When ``open_member_reserved`` is True the open-index member has already
        been added by ``reserve_open_slot`` (the atomic concurrency guard), so this
        call does NOT re-add it — avoiding a double ZADD. The unbounded (no
        ``max_concurrent``) path leaves it False and adds the member here.

        Additional writes:

        * ``ZREMRANGEBYSCORE(open_key, 0, now_ms)`` runs UNCONDITIONALLY on every
          call, purging open-index members whose deadline has passed — a
          SIGKILLed waiter's member would otherwise linger forever (the ZSET has
          no TTL).
        * an ATOMIC phantom self-heal (``_PENDING_PURGE_LUA``) runs first: it
          drops every group whose furthest question deadline has passed from
          ``pending_key`` + ``pending_deadline_key`` (leaving ``count_key`` to its
          ``idle_ttl``), SKIPPING this call's own group (which is about to become
          live). Reading the expired set inside the script keeps the correlated
          multi-index delete safe against a concurrent revive (invariant: a group
          with a live question is never purged).
        * ``ZADD pending_deadline_key {group: timeout_at_ms} GT`` — the group's
          entry in the parallel deadline index, extend-only so a later question
          with a SHORTER deadline never shortens it.
        * ``ZADD open_key {interaction_id: timeout_at_ms}`` for ALL formats,
          UNLESS ``open_member_reserved`` (the atomic guard already added it) — the
          member is removed on answer (``record_answer``) or cleanup
          (``prune_pending``).
        * ``count_key`` TTL takes this park's ``_key_ttl`` via ``EXPIRE ... NX`` +
          ``EXPIRE ... GT`` (set-or-extend-to-greater) on the same basis as the
          group stream, so it converges to the MAX horizon across the group's parks
          and a surviving state always has a live count — including a park whose
          state outlives ``idle_ttl``. The phantom-purge Lua reconciles
          genuinely-dead groups on the deadline index, so the count needs no shorter
          deadline of its own; the extend-only refresh makes ``current is None`` at
          decrement a true torn-index invariant violation rather than a silently-read
          zero.
        * when ``ticket`` is given (external format): ``SET ticket_key
          interaction_id EX ticket_ttl``, mapping the callback capability to this
          interaction. The ticket is never deleted; it expires on its TTL.
        * when the request is ``async`` (which always carries an ``expiry_at``):
          ``ZADD pending_expiry_key {interaction_id: expiry_at_ms}`` — the
          per-interaction deadline the expiry reaper keys on (a sync question sets no
          member). ``continuation_fingerprint`` (the async fire's captured key
          fingerprint, ``""`` for a gate-off fire) is denormalized onto the state
          hash so the answer/expiry path rebinds the continuation under the same
          authority the ask ran under."""
        group_key = self.group_key(request.group_id)
        state_key = self.state_key(request.interaction_id)
        count_key = self.count_key(request.group_id)
        state = InteractionState(status="pending", group_id=request.group_id, request=request)
        # Atomic phantom self-heal over the parallel deadline index, BEFORE the new
        # question is written (its deadline is in the future, so it is never a
        # purge target). redis-py's async ``eval`` stub types a non-awaitable
        # return; it is awaitable at runtime.
        await cast(
            "Awaitable[int]",
            r.eval(
                _PENDING_PURGE_LUA,
                2,
                self.pending_deadline_key,
                self.pending_key,
                str(_now_ms()),
                request.group_id,
            ),
        )
        # The sensitive flag rides the state hash as a denormalized ``"1"`` (absent
        # when false), so ``record_answer`` can gate the response-body write on a
        # single ``hget`` inside its WATCH loop without deserializing the request.
        state_mapping: dict[str, str] = {
            "status": state.status,
            "group_id": state.group_id,
            "request": request.model_dump_json(),
        }
        if request.sensitive:
            state_mapping["sensitive"] = "1"
        if request.audience is not None:
            # Denormalized like ``sensitive`` so the answered/removed events can carry
            # the question's audience from a single ``hget`` inside the claim's WATCH
            # loop, without deserializing the request — the tail-only SSE filters those
            # frames on it directly (a restricted caller sees only its own). Absent means
            # an unaddressed question (audience None).
            state_mapping["audience"] = request.audience
        if request.mode == "async":
            # Denormalized like ``sensitive`` so the atomic answer/expiry claim can write
            # the durable continuation-due record from a few ``hget``s inside its WATCH
            # loop, without deserializing the request. An async request always carries a
            # continuation tool + identity (model-validated); the fingerprint is the
            # captured fire key (``""`` for a gate-off fire, absent only pre-capture).
            assert request.continuation_tool is not None
            assert request.continuation_identity is not None
            state_mapping["continuation_tool"] = request.continuation_tool
            state_mapping["continuation_identity"] = request.continuation_identity
            if continuation_fingerprint is not None:
                state_mapping["continuation_fingerprint"] = continuation_fingerprint
            if thread_id is not None:
                # The conversation thread this park is bound to, denormalized like
                # ``continuation_tool`` so a terminal claim (record_answer/prune_pending)
                # can read WHICH thread-parks SET to drop this interaction from with a
                # single ``hget`` inside its WATCH loop. Present only for an async park
                # that carries a bound thread; a park with no thread never indexes.
                state_mapping["thread_id"] = thread_id
        new_media_ids = _media_ids_of(request)
        if new_media_ids:
            # This question's own served-media ids, comma-joined and denormalized like
            # ``sensitive`` so a terminal claim (record_answer/prune_pending) drops them
            # from the group media index from a single ``hget`` inside its WATCH loop
            # without deserializing the request. Absent when the question has no stored
            # media.
            state_mapping["media_ids"] = ",".join(new_media_ids)
        pipe = r.pipeline()
        pipe.zremrangebyscore(self.open_key, 0, _now_ms())
        pipe.xadd(
            group_key,
            {
                "interaction_id": request.interaction_id,
                "request": request.model_dump_json(),
            },
        )
        pipe.hset(state_key, mapping=state_mapping)
        pipe.incr(count_key)
        pipe.zadd(self.pending_key, {request.group_id: _created_ms(request)})
        pipe.zadd(self.pending_deadline_key, {request.group_id: _timeout_ms(request)}, gt=True)
        if request.mode == "async" and request.expiry_at is not None:
            # The per-interaction expiry deadline the reaper scans; removed on any
            # terminal exit (answer/expiry/prune). An async park always carries an
            # ``expiry_at``, so this guard is always true for async.
            pipe.zadd(self.pending_expiry_key, {request.interaction_id: _expiry_ms(request)})
        # The thread→interaction reverse index: only an async park with a bound thread
        # joins it (a background tool run passes no thread and indexes nothing). Rides
        # THIS same atomic pipeline as the state/pending/expiry writes so the index is
        # consistent with them; its TTL is set-or-extended below alongside the group
        # stream's, so it never expires before a park it indexes.
        thread_parks_key: str | None = None
        if request.mode == "async" and thread_id is not None:
            thread_parks_key = self.thread_parks_key(thread_id)
            pipe.sadd(thread_parks_key, request.interaction_id)
        if not open_member_reserved:
            # The atomic guard (``reserve_open_slot``) already added this member;
            # re-adding here would double-count the open index.
            pipe.zadd(self.open_key, {request.interaction_id: _timeout_ms(request)})
        if ticket is not None:
            if ticket_ttl is None:
                raise ValueError("add(): ticket given without ticket_ttl")
            pipe.set(self.ticket_key(ticket), request.interaction_id, ex=ticket_ttl)
        pipe.xadd(
            self.events_key,
            {
                "type": ADD_EVENT,
                "interaction_id": request.interaction_id,
                "group_id": request.group_id,
            },
            maxlen=_EVENTS_MAXLEN,
            approximate=True,
        )
        # Per-key TTLs from ``_key_ttl``: an async park beyond the idle horizon
        # survives to its expiry (plus margin), every other key stays at idle_ttl.
        # This park's own state hash takes its own horizon. The shared group stream
        # and count_key take a SET-OR-EXTEND-TO-GREATER TTL: ``EXPIRE ... NX`` sets
        # this horizon when the key has none yet (a group's first park — a bare
        # ``GT`` treats a no-expiry key as infinity and would leave it unbounded),
        # and ``EXPIRE ... GT`` raises an existing TTL only when this horizon is
        # longer. Together a concurrent add to the same group can only push these
        # keys LATER, so they converge to the MAX horizon across all the group's
        # parks regardless of commit order and can never be shrunk below a co-grouped
        # long park's horizon by a later short-horizon add. A surviving pending state
        # therefore always has a live count, so ``current is None`` at a decrement is
        # a genuine torn-index invariant violation (raise-worthy), never a spurious
        # miss at the expiry boundary.
        now_ms = _now_ms()
        new_ttl = _key_ttl(request, idle_ttl, now_ms, expiry_ttl_margin_seconds)
        pipe.expire(state_key, new_ttl)
        pipe.expire(group_key, new_ttl, nx=True)
        pipe.expire(group_key, new_ttl, gt=True)
        pipe.expire(count_key, new_ttl, nx=True)
        pipe.expire(count_key, new_ttl, gt=True)
        if thread_parks_key is not None:
            # The reverse index takes the SAME set-or-extend-to-greater TTL as the group
            # stream, so it outlives the longest-lived park it indexes: ``NX`` sets the
            # first horizon (a bare ``GT`` treats a no-expiry set as infinity and would
            # leave it unbounded), ``GT`` raises it only when this park's horizon is
            # longer. A later co-thread park can only push it LATER, never shrink it.
            pipe.expire(thread_parks_key, new_ttl, nx=True)
            pipe.expire(thread_parks_key, new_ttl, gt=True)
        # Media the group's questions reference must outlive no shorter than the group
        # stream: this question's own ids join the group media index, and every member
        # media key (plus the index) takes the same SET-OR-EXTEND-TO-GREATER TTL as the
        # group stream, so a co-grouped long park keeps the whole group's media alive.
        # One Lua call folds read-members + SADD-new + the index/key EXPIREs in, queued
        # into this write pipeline so it commits ATOMICALLY with the question — no window
        # where committed state carries media at the bootstrap TTL. Removal/answer paths
        # never delete media — it expires on this TTL.
        pipe.eval(
            _MEDIA_SET_OR_EXTEND_LUA,
            1,
            self.media_index_key(request.group_id),
            str(new_ttl),
            self.media_key(""),
            *new_media_ids,
        )
        await pipe.execute()

    async def reserve_open_slot(self, r: Redis, request: InteractionRequest, limit: int) -> bool:
        """Atomically reserve an open-index slot under the ``max_concurrent`` cap.

        Purges stale open members (deadline passed), then — in the SAME server
        round trip — admits this question by adding its open-index member ONLY
        while the live open count is below ``limit``. Returns ``True`` when the
        slot was reserved (the member is now in the open index), ``False`` when the
        cap is already full (nothing written). Because the check and the add are
        one atomic step, a concurrent burst admits exactly ``limit`` callers and
        refuses the rest — no unbounded overshoot.

        A ``True`` reservation adds the SAME member ``add`` would, so the caller
        must then invoke ``add(..., open_member_reserved=True)`` to avoid a double
        ZADD. redis-py's async ``eval`` stub types a non-awaitable return; it is
        awaitable at runtime."""
        reserved = await cast(
            "Awaitable[int]",
            r.eval(
                _OPEN_RESERVE_LUA,
                1,
                self.open_key,
                str(_now_ms()),
                str(limit),
                str(_timeout_ms(request)),
                request.interaction_id,
            ),
        )
        return bool(reserved)

    async def record_answer(
        self,
        r: Redis,
        response: InteractionResponse,
        group_id: str,
        reply_ttl: int,
        ticket: str | None = None,
        ticket_ttl: int | None = None,
        continuation_due_ttl: int | None = None,
        continuation_first_attempt_at_ms: int | None = None,
    ) -> bool:
        """Atomically claim and record an answer: mark answered, remove the
        open-index member, decrement the group's pending count (drop the group
        from the index at zero), wake the caller, append the answered-event. The
        reply key gets a short TTL so a late answer to a timed-out question
        expires instead of resurrecting it.

        DURABLE CONTINUATION OUTBOX: when the resolved interaction is an async park
        (its denormalized ``continuation_tool`` field is present), the SAME MULTI also
        writes the durable, FLOW-BLIND continuation-due record + its next-attempt index
        member. The enqueue commits together with the ``answered`` state change, so a
        crash can never leave a claimed answer with no due-record — closing the
        dual-write window a post-claim persist would leave. Every resolution door funnels
        through this claim, so all three (authenticated answer, callback answer, expiry
        reaper) are covered by construction. The caller supplies the record TTL +
        first-attempt score (``continuation_due_ttl`` / ``continuation_first_attempt_at_ms``);
        an async park resolved without them is a caller bug and raises, never a silent
        skip. A sync question (no ``continuation_tool``) writes no due-record.

        When ``ticket`` is given (the callback doors pass the resolved ticket +
        ``idle_ttl_seconds``): ``EXPIRE ticket_key ticket_ttl`` inside the MULTI,
        refreshing the idempotency window to match the answered state's lifetime
        so late provider retries still resolve the ticket and reach the
        already-answered path. The ticket is never deleted (EXPIRE on an
        already-expired key is a harmless no-op). The ``/answer`` and prune paths
        pass no ticket (no refresh).

        When the question was marked ``sensitive`` at ``add`` time, the answered
        state records ONLY ``{"status": "answered"}`` — the response body is never
        written into the durable hash. The reply-key RPUSH is unchanged, so the
        blocked waiter still receives the full answer; only the persisted record
        drops the body. A late duplicate to a sensitive question therefore takes
        the already-answered path with no body available — by design.

        Returns ``True`` when this call claimed the answer, ``False`` when the
        interaction was missing or already answered (a lost duplicate race) —
        in which case nothing is written and no caller is woken."""
        if ticket is not None and ticket_ttl is None:
            raise ValueError("record_answer(): ticket given without ticket_ttl")
        interaction_id = response.interaction_id
        state_key = self.state_key(interaction_id)
        count_key = self.count_key(group_id)
        reply_key = self.reply_key(interaction_id)
        response_json = response.model_dump_json()

        async with r.pipeline() as pipe:
            while True:
                try:
                    # Watch the count key too: a concurrent add() to the same
                    # group INCRs it, which must invalidate this transaction so
                    # the at-zero cleanup can't drop a group that just gained a
                    # new open question.
                    await pipe.watch(state_key, count_key)
                    # redis-py's async stubs type pre-MULTI pipeline reads with the
                    # sync (non-awaitable) return; the value is awaitable at runtime.
                    status = as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "status")))
                    if status is None or status == "answered":
                        await pipe.reset()
                        return False
                    sensitive = as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "sensitive"))) == "1"
                    # The question's audience rides the answered event so the tail-only
                    # SSE filters the frame directly (absent = an unaddressed question).
                    audience = as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "audience")))
                    # The question's own media ids, read from the denormalized ``media_ids``
                    # field, so the group's media index drops them as the question leaves
                    # pending (the media keys themselves are left to expire on their group
                    # TTL). Absent when the question referenced no stored media.
                    media_ids_field = as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "media_ids")))
                    media_ids = media_ids_field.split(",") if media_ids_field else []
                    # The conversation thread this park is bound to, read from the
                    # denormalized ``thread_id`` field so the thread→interaction reverse
                    # index drops this member as the question leaves pending. Absent for
                    # a sync question or a park with no bound thread.
                    park_thread_id = as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "thread_id")))
                    # An async park carries a denormalized continuation tool + identity
                    # (+ fingerprint); their presence is the signal to enqueue the durable
                    # continuation-due record in THIS claim's MULTI. Read them in the
                    # immediate (pre-MULTI) phase — MULTI queues, it cannot read.
                    continuation_tool = as_str(
                        await cast("Awaitable[str | None]", pipe.hget(state_key, "continuation_tool"))
                    )
                    continuation_identity: str | None = None
                    continuation_fingerprint: str | None = None
                    if continuation_tool is not None:
                        continuation_identity = as_str(
                            await cast("Awaitable[str | None]", pipe.hget(state_key, "continuation_identity"))
                        )
                        continuation_fingerprint = as_str(
                            await cast("Awaitable[str | None]", pipe.hget(state_key, "continuation_fingerprint"))
                        )
                        if continuation_due_ttl is None or continuation_first_attempt_at_ms is None:
                            raise RuntimeError(
                                f"async park {interaction_id!r} resolved without continuation-due timing "
                                "(caller must pass continuation_due_ttl + continuation_first_attempt_at_ms)"
                            )
                        if continuation_identity is None:
                            raise RuntimeError(
                                f"async park {interaction_id!r} missing denormalized continuation identity"
                            )
                    current = await pipe.get(count_key)
                    if current is None:
                        # count_key is set-or-extended to cover the group's
                        # longest-lived state, so a live (pending) state ALWAYS has a
                        # live count. A missing count here is a torn index, not a
                        # zero — raise, never guess.
                        raise RuntimeError(
                            f"pending count missing for group {group_id!r} with a live state {interaction_id!r}"
                        )
                    remaining = int(current) - 1
                    pipe.multi()
                    # A sensitive question persists only the answered status — the
                    # body is deliberately never written to the durable hash.
                    answered_mapping = {"status": "answered"}
                    if not sensitive:
                        answered_mapping["response"] = response_json
                    pipe.hset(state_key, mapping=answered_mapping)
                    pipe.decr(count_key)
                    pipe.zrem(self.open_key, interaction_id)
                    # Drop any async-expiry member so the reaper never re-fires an
                    # answered question (a no-op for a sync question, never a member).
                    pipe.zrem(self.pending_expiry_key, interaction_id)
                    if park_thread_id is not None:
                        # Drop this park from its thread's reverse index in the SAME MULTI
                        # as the expiry-member drop, so a thread delete racing this answer
                        # can never cancel an already-resolved park: both paths share this
                        # status gate. Absent for a sync question / an unbound park.
                        pipe.srem(self.thread_parks_key(park_thread_id), interaction_id)
                    if media_ids:
                        pipe.srem(self.media_index_key(group_id), *media_ids)
                    if ticket is not None:
                        # ``ticket_ttl`` is guaranteed non-None here (guarded at
                        # the top); pin it for the type checker.
                        assert ticket_ttl is not None
                        pipe.expire(self.ticket_key(ticket), ticket_ttl)
                        # Refresh the state key to the same window as the ticket:
                        # the idempotent already-answered path resolves the ticket
                        # AND reads the state, so a state expiring before the ticket
                        # would turn a late provider retry into a 404. Tying both to
                        # ticket_ttl keeps the answered state alive as long as the
                        # ticket can still resolve.
                        pipe.expire(state_key, ticket_ttl)
                    pipe.rpush(reply_key, response_json)
                    pipe.expire(reply_key, reply_ttl)
                    pipe.xadd(
                        self.events_key,
                        cast("dict[Any, Any]", _event_fields(ANSWERED_EVENT, interaction_id, group_id, audience)),
                        maxlen=_EVENTS_MAXLEN,
                        approximate=True,
                    )
                    if remaining <= 0:  # this was the group's last open question
                        pipe.zrem(self.pending_key, group_id)
                        pipe.zrem(self.pending_deadline_key, group_id)
                        pipe.delete(count_key)
                    if continuation_tool is not None:
                        # Enqueue the durable continuation-due outbox record ATOMICALLY
                        # with the claim. Validated non-None in the read phase above; pin
                        # for the type checker. The answer rides the record so a redelivery
                        # survives even a sensitive park (whose body is dropped from the
                        # answered state) and a since-expired state.
                        assert continuation_identity is not None
                        assert continuation_due_ttl is not None
                        assert continuation_first_attempt_at_ms is not None
                        due_key = self.continuation_due_key(interaction_id)
                        due_mapping = _continuation_due_mapping(
                            continuation_tool, continuation_identity, continuation_fingerprint or "", response.answer
                        )
                        pipe.hset(due_key, mapping=due_mapping)
                        pipe.expire(due_key, continuation_due_ttl)
                        pipe.zadd(self.continuation_due_index_key, {interaction_id: continuation_first_attempt_at_ms})
                    await pipe.execute()
                    return True
                except WatchError:
                    continue

    async def prune_pending(self, r: Redis, interaction_id: str, group_id: str) -> PruneResult:
        """Remove a still-open question that is being abandoned (cancel-cleanup or
        the timeout path). Status-gated exactly like ``record_answer``, INCLUDING
        its ``except WatchError: continue`` retry loop: an answer committing
        between the status read and EXEC fires WatchError, the retry then reads
        ``answered`` and returns ``"answered"`` cleanly.

        WATCHes the state key AND the count key — the count WATCH for the same
        reason ``record_answer`` has it: a concurrent ``add()`` to the group INCRs
        the count and must invalidate this transaction, or the at-zero cleanup
        would delete the count key and drop the group from the pending index while
        a just-added sibling is still open.

        Returns the end state, distinguishing the two no-op cases the caller must
        tell apart: ``"answered"`` when ``status`` is ``"answered"`` and ``"gone"``
        when the state key is missing/expired — both no-ops with NO writes (an
        answered interaction must never be pruned, a vanished one has nothing to
        prune). When still ``pending``, in one MULTI: delete the state key, ``ZREM
        open_key``, ``DECR`` the group count, and at zero delete the count key +
        ``ZREM`` the group from BOTH the pending index and the parallel
        pending-deadline index; then append an ``interaction.removed`` event so a
        live SSE consumer can drop the pruned question, and return ``"pruned"``."""
        state_key = self.state_key(interaction_id)
        count_key = self.count_key(group_id)

        async with r.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(state_key, count_key)
                    status = as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "status")))
                    if status is None:
                        await pipe.reset()
                        return "gone"
                    if status == "answered":
                        await pipe.reset()
                        return "answered"
                    # The question's audience rides the removed event so the tail-only
                    # SSE filters the frame directly (absent = an unaddressed question).
                    audience = as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "audience")))
                    # The question's own media ids drop from the group's media index as it
                    # leaves pending, read from the denormalized ``media_ids`` field; the media
                    # keys expire on their group TTL. Absent when the question had no stored media.
                    media_ids_field = as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "media_ids")))
                    media_ids = media_ids_field.split(",") if media_ids_field else []
                    # The bound conversation thread, read from the denormalized ``thread_id``
                    # field so the thread→interaction reverse index drops this member as the
                    # park is pruned. Absent for a sync question or a park with no thread.
                    park_thread_id = as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "thread_id")))
                    current = await pipe.get(count_key)
                    if current is None:
                        # count_key is set-or-extended to cover the group's
                        # longest-lived state, so a live (pending) state ALWAYS has a
                        # live count. A missing count here is a torn index, not a
                        # zero — raise, never guess.
                        raise RuntimeError(
                            f"pending count missing for group {group_id!r} with a live state {interaction_id!r}"
                        )
                    remaining = int(current) - 1
                    pipe.multi()
                    pipe.delete(state_key)
                    pipe.zrem(self.open_key, interaction_id)
                    # Drop any async-expiry member alongside the state (a no-op for
                    # a sync question, never a member).
                    pipe.zrem(self.pending_expiry_key, interaction_id)
                    if park_thread_id is not None:
                        # Drop this park from its thread's reverse index in the SAME MULTI
                        # as the expiry-member drop — the cascade a thread delete drives
                        # runs through here, so the index self-heals as each park is pruned.
                        pipe.srem(self.thread_parks_key(park_thread_id), interaction_id)
                    if media_ids:
                        pipe.srem(self.media_index_key(group_id), *media_ids)
                    pipe.decr(count_key)
                    if remaining <= 0:  # this was the group's last open question
                        pipe.zrem(self.pending_key, group_id)
                        pipe.zrem(self.pending_deadline_key, group_id)
                        pipe.delete(count_key)
                    pipe.xadd(
                        self.events_key,
                        cast("dict[Any, Any]", _event_fields(REMOVED_EVENT, interaction_id, group_id, audience)),
                        maxlen=_EVENTS_MAXLEN,
                        approximate=True,
                    )
                    await pipe.execute()
                    return "pruned"
                except WatchError:
                    continue

    async def cancel_thread_parks(self, r: Redis, thread_id: str) -> list[str]:
        """Cancel every async park bound to ``thread_id`` — the ONE cascade a conversation
        thread delete (admin-delete, forget-me, route-delete) fires so a parked ``ask_user``
        the deletion would ORPHAN is torn down instead of lingering (its expiry reaper later
        firing a continuation into a thread that no longer exists → a delivery retry storm,
        its channel correlation muting the guest's number until the ~24h deadline).

        Reads the thread's reverse-index members and runs the EXISTING ``prune_pending`` for
        each: status-gated and idempotent, it removes a still-pending park WITHOUT firing any
        continuation — deliberately not ``record_answer`` (which would enqueue a dead
        completion), so no continuation fires and no ``PARK_COMPLETION_FAILED`` is needed — and
        is a clean no-op on a park already answered or gone. A member whose state already
        vanished is skipped (nothing to prune); the snapshotted members are then SREM'd (NOT a
        blind key delete) so such an orphan member is reconciled off while a park added to the
        thread concurrently with the cascade keeps its member and stays cancellable on a retry.
        Returns the interaction ids read from the index.

        Idempotent: cancelling a thread with no parks (a missing set) is a no-op, and
        cancelling twice finds the set drained/absent the second time. The recovery is proven:
        once the interaction state is gone the answer-door returns not-found, and each channel
        bridges the next reply as a fresh turn and self-releases its correlation — so this
        cancellation is channel-blind and enumerates no channels."""
        key = self.thread_parks_key(thread_id)
        members = [as_str(member) for member in await cast("Awaitable[set[str | bytes]]", r.smembers(key))]
        for interaction_id in members:
            group_id = as_str(
                await cast("Awaitable[str | bytes | None]", r.hget(self.state_key(interaction_id), "group_id"))
            )
            if group_id is None:
                # The park's state already vanished (answered/expired/pruned): nothing to
                # prune, and the trailing delete reconciles the orphan member off.
                continue
            await self.prune_pending(r, interaction_id, group_id)
        # Remove ONLY the members we snapshotted (SREM, not a blind DELETE of the key): a park
        # added to this thread CONCURRENTLY with the cascade — between the smembers snapshot
        # above and here — keeps its own index member, so it stays cascade-cancellable on a
        # retry instead of being silently orphaned by wiping the whole set. prune_pending
        # already SREM'd each park it pruned (repeating is a no-op); this additionally
        # reconciles the snapshot's orphan members (state already gone, skipped above). The
        # set auto-deletes once its last member is removed; its TTL backstops either way.
        if members:
            await cast("Awaitable[int]", r.srem(key, *members))
        return members

    # -- reads ---------------------------------------------------------------

    async def resolve_ticket(self, r: Redis, ticket: str) -> str | None:
        """Return the interaction id a callback ticket maps to, or ``None`` when
        the ticket never existed or has expired (lookup-by-exact-key IS the
        comparison — no user-supplied string is compared in Python)."""
        return as_str(await cast("Awaitable[str | bytes | None]", r.get(self.ticket_key(ticket))))

    async def due_expiries(self, r: Redis, now: datetime) -> list[str]:
        """The interaction ids of async parks whose ``expiry_at`` is at or before
        ``now`` — the expiry reaper's work list. Reads the per-interaction expiry
        index by score; a member is removed from it only when the question leaves
        pending (answer/expiry/prune), so a lingering member for a
        vanished/answered state is re-reconciled by the reaper (claim returns
        no-op, the member is dropped)."""
        cutoff = int(now.timestamp() * 1000)
        raw = await r.zrangebyscore(self.pending_expiry_key, 0, cutoff)
        return [as_str(member) for member in raw]

    async def drop_expiry_member(self, r: Redis, interaction_id: str) -> None:
        """Remove ``interaction_id`` from the expiry index — the reaper's cleanup for
        a member whose state already vanished/answered (so the claim was a no-op),
        keeping the index from re-listing a dead member every pass."""
        await r.zrem(self.pending_expiry_key, interaction_id)

    async def continuation_fingerprint(self, r: Redis, interaction_id: str) -> str | None:
        """The async fire's captured key fingerprint stashed on the state hash
        (``""`` for a gate-off fire), or ``None`` when the question carries none (a
        sync question, or a state that has expired). The answer/expiry path passes
        it to the execution-identity bind that runs the stored continuation."""
        raw = await cast(
            "Awaitable[str | bytes | None]", r.hget(self.state_key(interaction_id), "continuation_fingerprint")
        )
        return as_str(raw)

    async def clear_continuation_due(self, r: Redis, interaction_id: str) -> None:
        """Delete the durable continuation-due record + drop its index member once
        ``run_tool`` has returned (the consumer durably applied the resume). Atomic
        so a reaper pass never reads a half-cleared record. A no-op
        on an already-cleared record (a redelivery the original fire raced to
        completion), so double-clear is harmless."""
        key = self.continuation_due_key(interaction_id)
        pipe = r.pipeline()
        pipe.delete(key)
        pipe.zrem(self.continuation_due_index_key, interaction_id)
        await pipe.execute()

    async def due_continuations(self, r: Redis, now: datetime) -> list[str]:
        """The interaction ids of continuation-due records whose next-attempt time is
        at or before ``now`` — the redelivery reaper's work list. A member survives
        until its continuation's ``run_tool`` returns (or its record TTL-expires and
        the retry-claim reconciles the orphan member off)."""
        cutoff = int(now.timestamp() * 1000)
        raw = await r.zrangebyscore(self.continuation_due_index_key, 0, cutoff)
        return [as_str(member) for member in raw]

    async def claim_continuation_retry(
        self, r: Redis, interaction_id: str, now: datetime, backoff_base_ms: int, backoff_cap_ms: int
    ) -> ContinuationDue | ContinuationRetryDrop | None:
        """Atomically claim a due continuation-due record for redelivery: advance its
        next-attempt score by an exponential backoff (base doubled per prior attempt,
        capped) and return the record to re-fire. Returns ``None`` when the member is
        not (or no longer) due — already cleared or already re-claimed this window by a
        racing reaper. Returns ``CONTINUATION_DROPPED`` when the member is an orphan
        whose record hash TTL-expired past its retention horizon (which this reconciles
        off the index): a permanent give-up the caller must surface loudly. redis-py's
        async ``eval`` stub types a non-awaitable return; it is awaitable at runtime."""
        raw = await cast(
            "Awaitable[list[Any] | str | bytes | None]",
            r.eval(
                _CONTINUATION_RETRY_CLAIM_LUA,
                2,
                self.continuation_due_index_key,
                self.continuation_due_key(interaction_id),
                interaction_id,
                str(int(now.timestamp() * 1000)),
                str(backoff_base_ms),
                str(backoff_cap_ms),
            ),
        )
        if not raw:
            return None
        if raw in (b"dropped", "dropped"):
            return CONTINUATION_DROPPED
        it = iter(cast("list[Any]", raw))
        fields = {as_str(k): as_str(v) for k, v in zip(it, it, strict=True)}
        return ContinuationDue(
            interaction_id=interaction_id,
            tool=fields["tool"],
            identity=fields["identity"],
            fingerprint=fields["fingerprint"],
            answer=json.loads(fields["answer"]),
            attempts=int(fields["attempts"]),
        )

    async def count_open(self, r: Redis) -> int:
        """The live open-question count: purge open-index members whose deadline
        has passed, then ``ZCARD``. All ``open_key`` access stays inside the store
        so no caller touches the key inline. The ``max_concurrent`` cap enforces
        itself atomically in ``reserve_open_slot``; this read serves callers that
        only need the current count."""
        await r.zremrangebyscore(self.open_key, 0, _now_ms())
        return await cast("Awaitable[int]", r.zcard(self.open_key))

    def _state_from_raw(self, raw: dict[str | bytes, str | bytes]) -> InteractionState | None:
        """Build an ``InteractionState`` from a raw state-hash mapping (as returned
        by ``HGETALL``), or ``None`` when the hash is empty (missing/expired)."""
        if not raw:
            return None
        fields = {as_str(k): as_str(v) for k, v in raw.items()}
        request = InteractionRequest.model_validate_json(fields["request"])
        response_json = fields.get("response")
        response = InteractionResponse.model_validate_json(response_json) if response_json else None
        return InteractionState(
            # Pydantic validates the stored status against the Literal at runtime.
            status=cast("Literal['pending', 'answered']", fields["status"]),
            group_id=fields["group_id"],
            request=request,
            response=response,
        )

    async def get_state(self, r: Redis, interaction_id: str) -> InteractionState | None:
        # redis-py's async stubs type ``hgetall`` with the sync (non-awaitable)
        # return; it is awaitable at runtime.
        raw = await cast("Awaitable[dict[str | bytes, str | bytes]]", r.hgetall(self.state_key(interaction_id)))
        return self._state_from_raw(raw)

    async def pending(self, r: Redis) -> list[InteractionRequest]:
        """The full pending-question set the paged list door serves, in
        ``pending_key`` score order (each group's most-recent question
        ``created_at``) then stream order within a group. Performs the same
        reconciliation an inline read would, so the door
        holds zero store-key knowledge:

        * a phantom group (its stream expired but it lingers in the index) is
          pruned from BOTH ``pending_key`` and ``pending_deadline_key`` and skipped;
        * an answered or missing state is skipped;
        * an abandoned pending question (past its deadline — e.g. a SIGKILLed
          waiter whose cleanup never ran) is pruned via ``prune_pending`` and
          skipped, so the badge/list stay honest.

        Each group's per-entry state reads are batched into ONE pipeline (a single
        round trip for the group's open questions) rather than an N+1 of
        per-question ``HGETALL`` calls."""
        now = datetime.now(UTC)
        pending: list[InteractionRequest] = []
        for raw_group in await r.zrange(self.pending_key, 0, -1):
            group_id = as_str(raw_group)
            entries = await r.xrange(self.group_key(group_id))
            if not entries:
                # The group's stream expired but lingered in the indexes — prune
                # the phantom from both so the badge/list don't count it.
                await r.zrem(self.pending_key, group_id)
                await r.zrem(self.pending_deadline_key, group_id)
                continue
            requests = [
                InteractionRequest.model_validate_json(as_str(fields.get("request") or fields.get(b"request")))
                for _entry_id, fields in entries
            ]
            # One pipeline for the whole group's state hashes — no N+1.
            pipe = r.pipeline()
            for req in requests:
                pipe.hgetall(self.state_key(req.interaction_id))
            raw_states = await pipe.execute()
            for req, raw in zip(requests, raw_states, strict=True):
                state = self._state_from_raw(raw)
                if state is None or state.status != "pending":
                    continue
                # An async park is NEVER pruned by this sync-deadline path: pruning
                # it would drop its continuation on the floor. Its lifetime is
                # governed by the expiry reaper (which fires the continuation) and
                # the idle TTL — so it always shows in the inbox until then.
                if state.request.mode != "async" and now >= state.request.timeout_at:
                    await self.prune_pending(r, req.interaction_id, group_id)
                    continue
                pending.append(req)
        return pending

    async def wait_for_reply(
        self, r: Redis, reply_to: str, timeout_seconds: float, grace_seconds: float
    ) -> InteractionResponse | None:
        """Block on the reply channel up to ``timeout_seconds``. Returns the
        recorded response, or ``None`` when the budget elapses with no answer.

        The BLPOP blocks legitimately for the whole ``timeout_seconds``, so its
        connection carries no socket read timeout (the caller strips it). To keep
        a black-holed redis from wedging the loop task forever, the BLPOP is wrapped
        in an outer ``asyncio.wait_for(timeout_seconds + grace_seconds)``: ``grace``
        is the slack past the server-side block window, passed in by the caller (the
        store holds no settings). A timeout there means the connection is presumed
        stalled — a loud ``RuntimeError``, DISTINCT from the normal no-answer path
        (BLPOP nil -> ``None`` -> ``InteractionTimeoutError`` in the caller), which
        is unchanged."""
        # redis-py's async stubs type ``blpop`` with the sync (non-awaitable)
        # return and an ``int`` timeout; at runtime it is awaitable and accepts a
        # float (fractional-second) timeout. The value is passed through untouched
        # — casting it to ``int`` would truncate a sub-second budget to 0, which
        # BLPOP reads as "block forever". The ignore only silences the stub's
        # int-only ``timeout`` kwarg; redis supports a float timeout natively.
        try:
            result = await asyncio.wait_for(
                cast(
                    "Awaitable[tuple[Any, Any] | None]",
                    r.blpop([reply_to], timeout=timeout_seconds),  # type: ignore[arg-type]
                ),
                timeout=timeout_seconds + grace_seconds,
            )
        except TimeoutError as exc:
            raise RuntimeError(
                "interactions reply wait: redis BLPOP returned nothing within "
                f"budget+{grace_seconds}s grace — connection presumed stalled"
            ) from exc
        if result is None:
            return None
        _, value = result
        return InteractionResponse.model_validate_json(as_str(value))
