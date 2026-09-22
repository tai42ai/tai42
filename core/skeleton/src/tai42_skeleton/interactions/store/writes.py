"""The durable question-lifecycle mutations.

Persist a new question, atomically reserve an open slot, claim-and-record an answer, prune an
abandoned question, and cascade-cancel a thread's parks.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from redis.asyncio import Redis
from redis.exceptions import WatchError
from tai42_contract.conversation_target import ConversationTargetKind
from tai42_contract.interactions import InteractionRequest, InteractionResponse, InteractionState
from tai42_contract.states import StateContext, SubjectCandidates

from . import events, records, scripts, serde, ttl
from .keys import _StoreKeys
from .records import WaitingOutcome

# The three end states ``prune_pending`` distinguishes: ``"pruned"`` deleted a
# still-pending question; ``"answered"`` found an answered status (no writes);
# ``"gone"`` found no state key at all (missing/expired, no writes).
PruneResult = Literal["pruned", "answered", "gone"]

# ``enqueue_kill``'s outcome. The three ``PruneResult`` states plus ``"skipped"``: the target was
# already resolved (answered/running) and the caller's ``act_on`` precondition excluded it, so the
# MULTI wrote NOTHING — the resolving answer's own continuation owns the run.
KillEnqueueResult = Literal["pruned", "answered", "gone", "skipped"]

# The kill's status precondition: the resolved target states a door may tear down. The whole-chain
# teardown walk (subject erase, thread/person/route delete) acts on an answered/running sibling too,
# so it never redelivers into a torn-down run; a SINGLE-park door (the expiry reaper's
# ``on_expiry="kill"`` branch, the per-interaction cancel) acts ONLY on a still-pending target, so a
# raced answer that resolved the park in the claim window is left to its own continuation.
KILL_ACT_ON_ANY: Final[frozenset[str]] = frozenset({"pending", "answered"})
KILL_ACT_ON_PENDING: Final[frozenset[str]] = frozenset({"pending"})

# Which door a park's answer/outcome is addressed to. ``"user"`` asks reserve from
# ``open_key`` under ``max_concurrent``; ``"caller"`` asks (and the waiting outcomes
# they leave) reserve from ``open_caller_key`` under ``max_concurrent_caller``. The two
# caps are independent. Denormalized onto the state hash (absent means ``"user"``) so a
# terminal claim / prune frees the RIGHT index member from a single ``hget``.
AskTo = Literal["user", "caller"]


@dataclass(frozen=True)
class _AnswerClaim:
    """The denormalized state-hash fields ``record_answer`` reads in its immediate (pre-MULTI) phase.

    Plus the group's post-decrement ``remaining`` count — the inputs the MULTI queue writes
    from.
    """

    sensitive: bool
    audience: str | None
    media_ids: list[str]
    park_thread_id: str | None
    to: AskTo
    subjects: str | None
    asked_by: list[str] | None
    delivery: str | None
    run_delivery_id: str | None
    continuation_tool: str | None
    continuation_identity: str | None
    continuation_fingerprint: str | None
    continuation_state_context: str | None
    remaining: int


@dataclass(frozen=True)
class _KillPrune:
    """The denormalized state-hash fields a kill's pending-park prune MULTI writes from.

    ``remaining`` is the group's post-decrement count (``None`` when the kill carries no group, so
    the count/pending-index cleanup is skipped — a subject-erase member reached without its group).
    """

    to: AskTo
    audience: str | None
    media_ids: list[str]
    park_thread_id: str | None
    subjects: str | None
    remaining: int | None


class _StoreWrites(_StoreKeys):
    def _open_key_for(self, to: AskTo) -> str:
        """The open-slot ZSET a ``to``-addressed entry reserves from — the two caps are independent."""
        return self.open_caller_key if to == "caller" else self.open_key

    @staticmethod
    def _addressed_to(value: str | None) -> AskTo:
        """Read the denormalized ``to`` field: ``"caller"`` only when explicitly stamped, else ``"user"``."""
        return "caller" if value == "caller" else "user"

    def _queue_subject_leave(self, pipe: Any, subjects: str | None, member: str) -> None:
        """Enqueue the ``SREM`` of ``member`` from every subject-parks set the descriptor addresses."""
        if subjects is None:
            return
        for target_kind, target_name, kind, key in records.iter_subject_keys(json.loads(subjects)):
            pipe.srem(self.subject_parks_key(target_kind, target_name, kind, key), member)

    async def add(
        self,
        r: Redis,
        request: InteractionRequest,
        idle_ttl: int,
        ticket: str | None = None,
        ticket_ttl: int | None = None,
        open_member_reserved: bool = False,
        continuation_fingerprint: str | None = None,
        expiry_ttl_margin_seconds: int = ttl._DEFAULT_EXPIRY_TTL_MARGIN_SECONDS,
        thread_id: str | None = None,
        to: AskTo = "user",
        delivery: dict[str, Any] | None = None,
        run_delivery_id: str | None = None,
    ) -> None:
        """Persist a new question and refresh its TTLs.

        Writes the stream entry, state, pending index + deadline index + count, the open-index
        ZSET member, add-event, and refreshed TTLs. The TTL refresh gives this question's own
        state hash its own ``_key_ttl`` — it SELF-COVERS, relying on no sibling to refresh it —
        and extends the shared group stream and ``count_key`` to the greatest horizon across the
        group's parks, so a still-open question always has a live group stream and count.

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

        An ATOMIC phantom self-heal (``_PENDING_PURGE_LUA``) runs first: it drops
        every group whose furthest question deadline has passed from ``pending_key``
        + ``pending_deadline_key`` (leaving ``count_key`` to its ``idle_ttl``),
        SKIPPING this call's own group (which is about to become live). Reading the
        expired set inside the script keeps the correlated multi-index delete safe
        against a concurrent revive (invariant: a group with a live question is never
        purged). When ``ticket`` is given (external format) it maps the callback
        capability to this interaction; the ticket is never deleted, it expires on
        its TTL. When the request is ``async`` (which always carries an ``expiry_at``)
        the per-interaction expiry member is added and ``continuation_*`` is
        denormalized onto the state hash so the answer/expiry path rebinds the
        continuation under the same authority the ask ran under.

        ``to`` addresses the ask — ``"user"`` (the default, an operator/inbox question)
        or ``"caller"`` (the parent run) — and selects the open-slot index this ask
        reserves from (``open_caller_key`` under ``max_concurrent_caller`` for a caller
        ask, ``open_key`` under ``max_concurrent`` for a user ask), so the two caps are
        independent. When the park carries a state context, the entry JOINS the subject
        index once per ``candidates.by_kind`` key (user asks included), so a subject
        listing, an erasure and a merge reach it without a SCAN. ``delivery`` (the run's
        durable out-of-band address) and ``run_delivery_id`` (the run's delivery
        identity) are denormalized when given, so the answer path copies them onto the
        continuation-due record for a detached redelivery.
        """
        # Atomic phantom self-heal over the parallel deadline index, BEFORE the new
        # question is written (its deadline is in the future, so it is never a
        # purge target). redis-py's async ``eval`` stub types a non-awaitable
        # return; it is awaitable at runtime.
        await cast(
            "Awaitable[int]",
            r.eval(
                scripts._PENDING_PURGE_LUA,
                2,
                self.pending_deadline_key,
                self.pending_key,
                str(ttl._now_ms()),
                request.group_id,
            ),
        )
        state_mapping = self._build_state_mapping(
            request, continuation_fingerprint, thread_id, to, delivery, run_delivery_id
        )
        new_media_ids = ttl._media_ids_of(request)
        pipe = r.pipeline()
        thread_parks_key = self._queue_record_writes(
            pipe, request, state_mapping, ticket, ticket_ttl, open_member_reserved, to
        )
        self._queue_ttls(pipe, request, idle_ttl, expiry_ttl_margin_seconds, thread_parks_key, new_media_ids)
        await pipe.execute()

    def _build_state_mapping(
        self,
        request: InteractionRequest,
        continuation_fingerprint: str | None,
        thread_id: str | None,
        to: AskTo = "user",
        delivery: dict[str, Any] | None = None,
        run_delivery_id: str | None = None,
    ) -> dict[str, str]:
        """Shape the durable state hash.

        Folds in the ``sensitive``/``audience``/``continuation_*``/``thread_id``/``media_ids``
        denormalizations a terminal claim reads from single ``hget``s without deserializing the
        request.
        """
        state = InteractionState(status="pending", group_id=request.group_id, request=request)
        state_mapping: dict[str, str] = {
            "status": state.status,
            "group_id": state.group_id,
            "request": request.model_dump_json(),
        }
        # The sensitive flag rides the state hash as a denormalized ``"1"`` (absent
        # when false), so ``record_answer`` can gate the response-body write on a
        # single ``hget`` inside its WATCH loop without deserializing the request.
        if request.sensitive:
            state_mapping["sensitive"] = "1"
        if request.audience is not None:
            # Denormalized like ``sensitive`` so the answered/removed events can carry
            # the question's audience from a single ``hget`` inside the claim's WATCH
            # loop. Absent means an unaddressed question (audience None).
            state_mapping["audience"] = request.audience
        if request.mode == "async":
            # An async request always carries a continuation tool + identity
            # (model-validated); the fingerprint is the captured fire key (``""`` for
            # a gate-off fire, absent only pre-capture).
            if request.continuation_tool is None:
                raise AssertionError
            if request.continuation_identity is None:
                raise AssertionError
            state_mapping["continuation_tool"] = request.continuation_tool
            state_mapping["continuation_identity"] = request.continuation_identity
            if continuation_fingerprint is not None:
                state_mapping["continuation_fingerprint"] = continuation_fingerprint
            if request.continuation_state_context is not None:
                # Denormalized so the durable continuation-due record carries the
                # original door's state context into an at-least-once REDELIVERY
                # (which never re-reads the request). Absent when the park ran under
                # no state context.
                state_mapping["continuation_state_context"] = request.continuation_state_context.model_dump_json()
            if thread_id is not None:
                # The conversation thread this park is bound to, denormalized so a
                # terminal claim can read WHICH thread-parks SET to drop this
                # interaction from with a single ``hget``. Present only for an async
                # park that carries a bound thread.
                state_mapping["thread_id"] = thread_id
        new_media_ids = ttl._media_ids_of(request)
        if new_media_ids:
            # This question's own served-media ids, comma-joined and denormalized so a
            # terminal claim drops them from the group media index from a single
            # ``hget``. Absent when the question has no stored media.
            state_mapping["media_ids"] = ",".join(new_media_ids)
        self._denormalize_addressing(state_mapping, request, to, delivery, run_delivery_id)
        return state_mapping

    @staticmethod
    def _denormalize_addressing(
        state_mapping: dict[str, str],
        request: InteractionRequest,
        to: AskTo,
        delivery: dict[str, Any] | None,
        run_delivery_id: str | None,
    ) -> None:
        """Fold the addressing/subject/delivery denormalizations onto the state hash.

        ``to`` rides only for a caller ask (absent means the default ``"user"``), so a terminal
        claim / prune frees the RIGHT open-slot index and every read surface gates on it from a
        single ``hget``. ``subjects`` is the descriptor the entry is indexed under, so any path that
        deletes the state key drops it from every subject-parks set without re-deriving from the
        full request. ``asked_by`` (the parking run's call chain), ``delivery`` (the run's durable
        address, JSON) and ``run_delivery_id`` are copied onto the continuation-due record by the
        answer path so the reaper's detached redelivery restores the chain and binds the address
        without re-reading the request.
        """
        if to != "user":
            state_mapping["to"] = to
        if request.continuation_state_context is not None:
            state_mapping["subjects"] = json.dumps(
                records.subjects_descriptor(request.continuation_state_context.candidates)
            )
        if request.asked_by:
            state_mapping["asked_by"] = json.dumps(request.asked_by)
        if delivery is not None:
            state_mapping["delivery"] = json.dumps(delivery)
        if run_delivery_id is not None:
            state_mapping["run_delivery_id"] = run_delivery_id

    def _queue_record_writes(
        self,
        pipe: Any,
        request: InteractionRequest,
        state_mapping: dict[str, str],
        ticket: str | None,
        ticket_ttl: int | None,
        open_member_reserved: bool,
        to: AskTo,
    ) -> str | None:
        """Enqueue the record + index writes; return the ``thread_parks_key`` (or ``None``) for the TTL step.

        Writes the stream, state, count, pending + deadline indexes, expiry member, thread-park
        member, subject index members, open member, ticket, and add-event.
        """
        group_key = self.group_key(request.group_id)
        state_key = self.state_key(request.interaction_id)
        count_key = self.count_key(request.group_id)
        open_key = self._open_key_for(to)
        # ``ZREMRANGEBYSCORE(open_key, 0, now_ms)`` runs UNCONDITIONALLY on every call,
        # purging open-index members whose deadline has passed — a SIGKILLed waiter's
        # member would otherwise linger forever (the ZSET has no TTL). The purge targets
        # the same index (user or caller) this ask reserves from.
        pipe.zremrangebyscore(open_key, 0, ttl._now_ms())
        pipe.xadd(
            group_key,
            {
                "interaction_id": request.interaction_id,
                "request": request.model_dump_json(),
            },
        )
        pipe.hset(state_key, mapping=state_mapping)
        pipe.incr(count_key)
        pipe.zadd(self.pending_key, {request.group_id: ttl._created_ms(request)})
        # Extend-only so a later question with a SHORTER deadline never shortens it.
        pipe.zadd(self.pending_deadline_key, {request.group_id: ttl._timeout_ms(request)}, gt=True)
        if request.mode == "async" and request.expiry_at is not None:
            # The per-interaction expiry deadline the reaper scans; removed on any
            # terminal exit. An async park always carries an ``expiry_at``.
            pipe.zadd(self.pending_expiry_key, {request.interaction_id: ttl._expiry_ms(request)})
        # The thread→interaction reverse index: only an async park with a bound thread
        # joins it (the ``thread_id`` denormalized above is the exact condition). Rides
        # THIS same atomic pipeline; its TTL is set-or-extended below.
        park_thread_id = state_mapping.get("thread_id")
        thread_parks_key: str | None = None
        if park_thread_id is not None:
            thread_parks_key = self.thread_parks_key(park_thread_id)
            pipe.sadd(thread_parks_key, request.interaction_id)
        subjects_field = state_mapping.get("subjects")
        if subjects_field is not None:
            # Join the subject index for each of the run's subject keys (user asks
            # included): the interaction id in each per-subject SET, and the scope
            # in each ``(kind, key)`` scopes SET. Rides THIS atomic pipeline; the SET
            # TTLs are set-or-extended below.
            descriptor = json.loads(subjects_field)
            for target_kind, target_name, kind, key in records.iter_subject_keys(descriptor):
                pipe.sadd(self.subject_parks_key(target_kind, target_name, kind, key), request.interaction_id)
                pipe.sadd(self.subject_scopes_key(kind, key), f"{target_kind}:{target_name}")
        if not open_member_reserved:
            # The atomic guard already added this member; re-adding here would
            # double-count the open index. The member joins the index this ask's ``to``
            # selects, so the user and caller caps stay independent.
            pipe.zadd(open_key, {request.interaction_id: ttl._timeout_ms(request)})
        if ticket is not None:
            if ticket_ttl is None:
                raise ValueError("add(): ticket given without ticket_ttl")
            pipe.set(self.ticket_key(ticket), request.interaction_id, ex=ticket_ttl)
        pipe.xadd(
            self.events_key,
            {
                "type": events.ADD_EVENT,
                "interaction_id": request.interaction_id,
                "group_id": request.group_id,
            },
            maxlen=events._EVENTS_MAXLEN,
            approximate=True,
        )
        return thread_parks_key

    def _queue_ttls(
        self,
        pipe: Any,
        request: InteractionRequest,
        idle_ttl: int,
        expiry_margin: int,
        thread_parks_key: str | None,
        media_ids: list[str],
    ) -> None:
        """Enqueue the per-key TTL refreshes.

        This park's own state hash to its own horizon, the shared group stream + count (and
        thread-parks set) set-or-extended to the greater horizon (``EXPIRE NX`` + ``EXPIRE GT``),
        and the media set-or-extend Lua over the group's media index and keys.
        """
        group_key = self.group_key(request.group_id)
        state_key = self.state_key(request.interaction_id)
        count_key = self.count_key(request.group_id)
        now_ms = ttl._now_ms()
        new_ttl = ttl._key_ttl(request, idle_ttl, now_ms, expiry_margin)
        pipe.expire(state_key, new_ttl)
        # ``NX`` sets this horizon when the key has none yet (a group's first park — a
        # bare ``GT`` treats a no-expiry key as infinity and would leave it unbounded);
        # ``GT`` raises an existing TTL only when this horizon is longer. Together the
        # shared keys converge to the MAX horizon across all the group's parks
        # regardless of commit order and can never be shrunk below a co-grouped long
        # park's horizon by a later short-horizon add.
        pipe.expire(group_key, new_ttl, nx=True)
        pipe.expire(group_key, new_ttl, gt=True)
        pipe.expire(count_key, new_ttl, nx=True)
        pipe.expire(count_key, new_ttl, gt=True)
        if thread_parks_key is not None:
            # The reverse index takes the SAME set-or-extend-to-greater TTL as the
            # group stream, so it outlives the longest-lived park it indexes.
            pipe.expire(thread_parks_key, new_ttl, nx=True)
            pipe.expire(thread_parks_key, new_ttl, gt=True)
        if request.continuation_state_context is not None:
            # The subject index sets take the SAME set-or-extend-to-greater TTL, so they
            # outlive the longest-lived entry they index; drained empty they simply
            # expire. A waiting outcome re-extends them when it joins, past the park's
            # own horizon.
            for target_kind, target_name, kind, key in records.iter_subject_keys(
                records.subjects_descriptor(request.continuation_state_context.candidates)
            ):
                parks_key = self.subject_parks_key(target_kind, target_name, kind, key)
                scopes_key = self.subject_scopes_key(kind, key)
                pipe.expire(parks_key, new_ttl, nx=True)
                pipe.expire(parks_key, new_ttl, gt=True)
                pipe.expire(scopes_key, new_ttl, nx=True)
                pipe.expire(scopes_key, new_ttl, gt=True)
        # Media the group's questions reference must outlive no shorter than the group
        # stream: this question's own ids join the group media index, and every member
        # media key (plus the index) takes the same SET-OR-EXTEND-TO-GREATER TTL. One
        # Lua call folds read-members + SADD-new + the index/key EXPIREs in, queued into
        # this write pipeline so it commits ATOMICALLY with the question.
        pipe.eval(
            scripts._MEDIA_SET_OR_EXTEND_LUA,
            1,
            self.media_index_key(request.group_id),
            str(new_ttl),
            self.media_key(""),
            *media_ids,
        )

    async def reserve_open_slot(self, r: Redis, request: InteractionRequest, limit: int, to: AskTo = "user") -> bool:
        """Atomically reserve an open-index slot under the cap for this ask's ``to``.

        Purges stale open members (deadline passed), then — in the SAME server
        round trip — admits this question by adding its open-index member ONLY
        while the live open count is below ``limit``. Returns ``True`` when the
        slot was reserved (the member is now in the open index), ``False`` when the
        cap is already full (nothing written). Because the check and the add are
        one atomic step, a concurrent burst admits exactly ``limit`` callers and
        refuses the rest — no unbounded overshoot.

        ``to`` selects the index and thus the cap: ``"caller"`` reserves from
        ``open_caller_key`` (bound by ``max_concurrent_caller``), ``"user"`` from
        ``open_key`` (bound by ``max_concurrent``); the two are independent, so a full
        caller cap never refuses a user ask and vice versa. A ``True`` reservation adds
        the SAME member ``add`` would, so the caller must then invoke ``add(...,
        open_member_reserved=True, to=to)`` to avoid a double ZADD. redis-py's async
        ``eval`` stub types a non-awaitable return; it is awaitable at runtime.
        """
        reserved = await cast(
            "Awaitable[int]",
            r.eval(
                scripts._OPEN_RESERVE_LUA,
                1,
                self._open_key_for(to),
                str(ttl._now_ms()),
                str(limit),
                str(ttl._timeout_ms(request)),
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
        """Atomically claim and record an answer.

        Marks answered, removes the open-index member, decrements the group's pending count
        (dropping the group from the index at zero), wakes the caller, appends the
        answered-event. The reply key gets a short TTL so a late answer to a timed-out question
        expires instead of resurrecting it.

        DURABLE CONTINUATION OUTBOX: when the resolved interaction is an async park
        (its denormalized ``continuation_tool`` field is present), the SAME MULTI also
        writes the durable, FLOW-BLIND continuation-due record + its next-attempt index
        member. The enqueue commits together with the ``answered`` state change, so a
        crash can never leave a claimed answer with no due-record. Every resolution door
        funnels through this claim, so all three (authenticated answer, callback answer,
        expiry reaper) are covered by construction. The caller supplies the record TTL +
        first-attempt score; an async park resolved without them is a caller bug and
        raises, never a silent skip. A sync question writes no due-record.

        When ``ticket`` is given (the callback doors pass the resolved ticket +
        ``idle_ttl_seconds``): ``EXPIRE ticket_key ticket_ttl`` inside the MULTI,
        refreshing the idempotency window to match the answered state's lifetime
        so late provider retries still resolve the ticket and reach the
        already-answered path. The ticket is never deleted. The ``/answer`` and
        prune paths pass no ticket (no refresh).

        When the question was marked ``sensitive`` at ``add`` time, the answered
        state records ONLY ``{"status": "answered"}`` — the response body is never
        written into the durable hash. The reply-key RPUSH is unchanged, so the
        blocked waiter still receives the full answer; only the persisted record
        drops the body.

        Returns ``True`` when this call claimed the answer, ``False`` when the
        interaction was missing or already answered (a lost duplicate race) —
        in which case nothing is written and no caller is woken.
        """
        if ticket is not None and ticket_ttl is None:
            raise ValueError("record_answer(): ticket given without ticket_ttl")
        interaction_id = response.interaction_id
        state_key = self.state_key(interaction_id)
        count_key = self.count_key(group_id)

        async with r.pipeline() as pipe:
            while True:
                try:
                    # Watch the count key too: a concurrent add() to the same
                    # group INCRs it, which must invalidate this transaction so
                    # the at-zero cleanup can't drop a group that just gained a
                    # new open question.
                    await pipe.watch(state_key, count_key)
                    claim = await self._read_answer_claim(
                        pipe,
                        state_key,
                        count_key,
                        group_id,
                        interaction_id,
                        continuation_due_ttl,
                        continuation_first_attempt_at_ms,
                    )
                    if claim is None:
                        await pipe.reset()
                        return False
                    pipe.multi()
                    self._queue_answer_writes(
                        pipe,
                        claim,
                        response,
                        group_id,
                        reply_ttl,
                        ticket,
                        ticket_ttl,
                        continuation_due_ttl,
                        continuation_first_attempt_at_ms,
                    )
                    await pipe.execute()
                except WatchError:
                    continue
                else:
                    return True

    async def _read_answer_claim(
        self,
        pipe: Any,
        state_key: str,
        count_key: str,
        group_id: str,
        interaction_id: str,
        continuation_due_ttl: int | None,
        continuation_first_attempt_at_ms: int | None,
    ) -> _AnswerClaim | None:
        """Read + validate the denormalized fields the claim needs.

        Reads the status gate plus the denormalized ``sensitive``/``audience``/``media_ids``/
        ``thread_id``/``continuation_*`` fields and the post-decrement ``remaining`` count,
        returning a frozen ``_AnswerClaim`` or ``None`` when the status is missing/``answered``.
        MULTI queues, it cannot read — so this is the immediate (pre-MULTI) phase.
        """
        # redis-py's async stubs type pre-MULTI pipeline reads with the sync
        # (non-awaitable) return; the value is awaitable at runtime.
        status = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "status")))
        if status is None or status == "answered":
            return None
        sensitive = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "sensitive"))) == "1"
        # The question's audience rides the answered event so the tail-only SSE
        # filters the frame directly (absent = an unaddressed question).
        audience = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "audience")))
        # The question's own media ids, from the denormalized ``media_ids`` field, so
        # the group's media index drops them as the question leaves pending.
        media_ids_field = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "media_ids")))
        media_ids = media_ids_field.split(",") if media_ids_field else []
        # The conversation thread this park is bound to, so the thread→interaction
        # reverse index drops this member. Absent for a sync question / unbound park.
        park_thread_id = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "thread_id")))
        # The ask's addressing, so the answer frees the RIGHT open-slot index (caller
        # vs user). Absent means the default ``"user"``.
        to = self._addressed_to(serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "to"))))
        # The subject descriptor + the run's delivery address/identity, denormalized at
        # add time: the continuation-due record copies delivery + run_delivery_id so a
        # detached redelivery binds the run's address without re-reading the request;
        # ``subjects`` rides the claim only so the record stays self-describing.
        subjects = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "subjects")))
        asked_by_field = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "asked_by")))
        asked_by = json.loads(asked_by_field) if asked_by_field is not None else None
        delivery = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "delivery")))
        run_delivery_id = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "run_delivery_id")))
        # An async park carries a denormalized continuation tool + identity (+
        # fingerprint); their presence is the signal to enqueue the durable
        # continuation-due record in THIS claim's MULTI.
        continuation_tool = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "continuation_tool")))
        continuation_identity: str | None = None
        continuation_fingerprint: str | None = None
        continuation_state_context: str | None = None
        if continuation_tool is not None:
            continuation_identity = serde.as_str(
                await cast("Awaitable[str | None]", pipe.hget(state_key, "continuation_identity"))
            )
            continuation_fingerprint = serde.as_str(
                await cast("Awaitable[str | None]", pipe.hget(state_key, "continuation_fingerprint"))
            )
            continuation_state_context = serde.as_str(
                await cast("Awaitable[str | None]", pipe.hget(state_key, "continuation_state_context"))
            )
            if continuation_due_ttl is None or continuation_first_attempt_at_ms is None:
                raise RuntimeError(
                    f"async park {interaction_id!r} resolved without continuation-due timing "
                    "(caller must pass continuation_due_ttl + continuation_first_attempt_at_ms)"
                )
            if continuation_identity is None:
                raise RuntimeError(f"async park {interaction_id!r} missing denormalized continuation identity")
        current = await pipe.get(count_key)
        if current is None:
            # count_key is set-or-extended to cover the group's longest-lived state, so
            # a live (pending) state ALWAYS has a live count. A missing count here is a
            # torn index, not a zero — raise, never guess.
            raise RuntimeError(f"pending count missing for group {group_id!r} with a live state {interaction_id!r}")
        return _AnswerClaim(
            sensitive=sensitive,
            audience=audience,
            media_ids=media_ids,
            park_thread_id=park_thread_id,
            to=to,
            subjects=subjects,
            asked_by=asked_by,
            delivery=delivery,
            run_delivery_id=run_delivery_id,
            continuation_tool=continuation_tool,
            continuation_identity=continuation_identity,
            continuation_fingerprint=continuation_fingerprint,
            continuation_state_context=continuation_state_context,
            remaining=int(current) - 1,
        )

    def _queue_answer_writes(
        self,
        pipe: Any,
        claim: _AnswerClaim,
        response: InteractionResponse,
        group_id: str,
        reply_ttl: int,
        ticket: str | None,
        ticket_ttl: int | None,
        continuation_due_ttl: int | None,
        continuation_first_attempt_at_ms: int | None,
    ) -> None:
        """Enqueue the MULTI writes for a claimed answer.

        The answered-state ``hset``, ``decr``, open/expiry/thread/media index removals, ticket +
        state refresh, the reply ``rpush`` + ``expire``, the answered event, the at-zero group
        cleanup, and the durable continuation-due outbox enqueue.
        """
        interaction_id = response.interaction_id
        state_key = self.state_key(interaction_id)
        count_key = self.count_key(group_id)
        reply_key = self.reply_key(interaction_id)
        response_json = response.model_dump_json()
        # A sensitive question persists only the answered status — the body is
        # deliberately never written to the durable hash.
        answered_mapping = {"status": "answered"}
        if not claim.sensitive:
            answered_mapping["response"] = response_json
        pipe.hset(state_key, mapping=answered_mapping)
        pipe.decr(count_key)
        # Free the ask's slot from the index it reserved from (caller vs user). The
        # subject index member is DELIBERATELY kept: the entry moves ``asking`` →
        # ``running`` (its continuation-due record) and stays listable until the run
        # terminates or is torn down.
        pipe.zrem(self._open_key_for(claim.to), interaction_id)
        # Drop any async-expiry member so the reaper never re-fires an answered
        # question (a no-op for a sync question, never a member).
        pipe.zrem(self.pending_expiry_key, interaction_id)
        if claim.park_thread_id is not None:
            # Drop this park from its thread's reverse index in the SAME MULTI as the
            # expiry-member drop, so a thread delete racing this answer can never
            # cancel an already-resolved park: both paths share this status gate.
            pipe.srem(self.thread_parks_key(claim.park_thread_id), interaction_id)
        if claim.media_ids:
            pipe.srem(self.media_index_key(group_id), *claim.media_ids)
        if ticket is not None:
            # ``ticket_ttl`` is guaranteed non-None here (guarded at the top); pin it
            # for the type checker.
            assert ticket_ttl is not None  # noqa: S101 (type-narrowing invariant guaranteed above; assert keeps the complexity floor)
            pipe.expire(self.ticket_key(ticket), ticket_ttl)
            # Refresh the state key to the same window as the ticket: the idempotent
            # already-answered path resolves the ticket AND reads the state, so a state
            # expiring before the ticket would turn a late provider retry into a 404.
            pipe.expire(state_key, ticket_ttl)
        pipe.rpush(reply_key, response_json)
        pipe.expire(reply_key, reply_ttl)
        pipe.xadd(
            self.events_key,
            cast(
                "dict[Any, Any]", events._event_fields(events.ANSWERED_EVENT, interaction_id, group_id, claim.audience)
            ),
            maxlen=events._EVENTS_MAXLEN,
            approximate=True,
        )
        if claim.remaining <= 0:  # this was the group's last open question
            pipe.zrem(self.pending_key, group_id)
            pipe.zrem(self.pending_deadline_key, group_id)
            pipe.delete(count_key)
        if claim.continuation_tool is not None:
            # Enqueue the durable continuation-due outbox record ATOMICALLY with the
            # claim. Validated non-None in the read phase above; pin for the type
            # checker. The answer rides the record so a redelivery survives even a
            # sensitive park (whose body is dropped) and a since-expired state.
            assert claim.continuation_identity is not None  # noqa: S101 (type-narrowing invariant guaranteed above; assert keeps the complexity floor)
            assert continuation_due_ttl is not None  # noqa: S101 (type-narrowing invariant guaranteed above; assert keeps the complexity floor)
            assert continuation_first_attempt_at_ms is not None  # noqa: S101 (type-narrowing invariant guaranteed above; assert keeps the complexity floor)
            due_key = self.continuation_due_key(interaction_id)
            due_mapping = records._continuation_due_mapping(
                claim.continuation_tool,
                claim.continuation_identity,
                claim.continuation_fingerprint or "",
                response.answer,
                claim.continuation_state_context,
                claim.asked_by,
                claim.delivery,
                claim.run_delivery_id,
            )
            pipe.hset(due_key, mapping=due_mapping)
            pipe.expire(due_key, continuation_due_ttl)
            pipe.zadd(self.continuation_due_index_key, {interaction_id: continuation_first_attempt_at_ms})

    async def prune_pending(
        self, r: Redis, interaction_id: str, group_id: str, *, reason: str | None = None
    ) -> PruneResult:
        """Remove a still-open question that is being abandoned (cancel-cleanup or the timeout path).

        Status-gated exactly like ``record_answer``, INCLUDING its ``except WatchError:
        continue`` retry loop: an answer committing between the status read and EXEC fires
        WatchError, the retry then reads ``answered`` and returns ``"answered"`` cleanly.

        ``reason`` TAGS the emitted ``interaction.removed`` event with WHY the question
        left pending (``"cancelled"`` for the operator per-interaction cancel door); it
        rides the event so a live operator surface can distinguish a deliberate withdrawal
        from a timeout/expiry removal. It defaults to ``None`` — the timeout path and the
        thread-delete cascade emit an UNTAGGED removed event.

        WATCHes the state key AND the count key — the count WATCH for the same
        reason ``record_answer`` has it: a concurrent ``add()`` to the group INCRs
        the count and must invalidate this transaction, or the at-zero cleanup
        would delete the count key and drop the group from the pending index while
        a just-added sibling is still open.

        Returns the end state, distinguishing the two no-op cases the caller must
        tell apart: ``"answered"`` when ``status`` is ``"answered"`` and ``"gone"``
        when the state key is missing/expired — both no-ops with NO writes. When
        still ``pending``, in one MULTI: delete the state key, ``ZREM open_key``,
        ``DECR`` the group count, and at zero delete the count key + ``ZREM`` the
        group from BOTH the pending index and the parallel pending-deadline index;
        then append an ``interaction.removed`` event and return ``"pruned"``.
        """
        state_key = self.state_key(interaction_id)
        count_key = self.count_key(group_id)

        async with r.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(state_key, count_key)
                    status = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "status")))
                    if status is None:
                        await pipe.reset()
                        return "gone"
                    if status == "answered":
                        await pipe.reset()
                        return "answered"
                    # The question's audience rides the removed event so the tail-only
                    # SSE filters the frame directly (absent = an unaddressed question).
                    audience = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "audience")))
                    # The question's own media ids drop from the group's media index as
                    # it leaves pending; the media keys expire on their group TTL.
                    media_ids_field = serde.as_str(
                        await cast("Awaitable[str | None]", pipe.hget(state_key, "media_ids"))
                    )
                    media_ids = media_ids_field.split(",") if media_ids_field else []
                    # The bound conversation thread, so the thread→interaction reverse
                    # index drops this member as the park is pruned.
                    park_thread_id = serde.as_str(
                        await cast("Awaitable[str | None]", pipe.hget(state_key, "thread_id"))
                    )
                    # The ask's addressing + subject descriptor, so the prune frees the
                    # RIGHT open-slot index and drops the entry from every subject-parks
                    # set it joined.
                    to = self._addressed_to(
                        serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "to")))
                    )
                    subjects = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "subjects")))
                    current = await pipe.get(count_key)
                    if current is None:
                        # count_key is set-or-extended to cover the group's
                        # longest-lived state, so a live (pending) state ALWAYS has a
                        # live count. A missing count here is a torn index, not a zero.
                        raise RuntimeError(
                            f"pending count missing for group {group_id!r} with a live state {interaction_id!r}"
                        )
                    remaining = int(current) - 1
                    pipe.multi()
                    pipe.delete(state_key)
                    pipe.zrem(self._open_key_for(to), interaction_id)
                    # Drop any async-expiry member alongside the state (a no-op for a
                    # sync question, never a member).
                    pipe.zrem(self.pending_expiry_key, interaction_id)
                    if park_thread_id is not None:
                        # Drop this park from its thread's reverse index in the SAME
                        # MULTI — the cascade a thread delete drives runs through here,
                        # so the index self-heals as each park is pruned.
                        pipe.srem(self.thread_parks_key(park_thread_id), interaction_id)
                    # Drop this entry from every subject-parks set it joined, in the
                    # SAME MULTI as the state delete — so the subject index leaves
                    # exactly where the state key is deleted.
                    self._queue_subject_leave(pipe, subjects, interaction_id)
                    if media_ids:
                        pipe.srem(self.media_index_key(group_id), *media_ids)
                    pipe.decr(count_key)
                    if remaining <= 0:  # this was the group's last open question
                        pipe.zrem(self.pending_key, group_id)
                        pipe.zrem(self.pending_deadline_key, group_id)
                        pipe.delete(count_key)
                    pipe.xadd(
                        self.events_key,
                        cast(
                            "dict[Any, Any]",
                            events._event_fields(
                                events.REMOVED_EVENT, interaction_id, group_id, audience, reason=reason
                            ),
                        ),
                        maxlen=events._EVENTS_MAXLEN,
                        approximate=True,
                    )
                    await pipe.execute()
                except WatchError:
                    continue
                else:
                    return "pruned"

    async def enqueue_kill(
        self,
        r: Redis,
        interaction_id: str,
        group_id: str | None,
        *,
        delivery: dict[str, Any] | None,
        run_delivery_id: str | None,
        subjects: dict[str, Any] | None,
        reason: str,
        kill_due_ttl: int,
        first_attempt_at_ms: int,
        deadline_ms: int,
        act_on: frozenset[str] = KILL_ACT_ON_ANY,
    ) -> KillEnqueueResult:
        """The whole-chain kill's teardown MULTI: prune the park, clear its continuation-due, enqueue the kill-due.

        Status-gated exactly like :meth:`prune_pending`, atomically under WATCH — and, when the
        target's resolved state is one the caller's ``act_on`` precondition permits, in the SAME MULTI:

        * a PENDING park is pruned (state, open/expiry/thread/subject/media indexes, count, removed
          event), returning ``"pruned"``; an ``answered`` park is not re-pruned (``"answered"``);
          a running entry whose state hash aged out while its continuation-due record still stands is
          cleared (the due record deleted, the kill-due written) and returns ``"pruned"`` too, so the
          caller tears it down now rather than deferring to the reaper; a missing state with NO
          continuation-due record is ``"gone"`` (nothing written);
        * the interaction's continuation-due record + its due-index member are deleted
          UNCONDITIONALLY of the ``pending`` status, so a buffered/answered sibling reached by the
          whole-chain walk has its live due record cleared and nothing redelivers into a run whose
          driver dropped its tombstones;
        * a durable kill-due record + its next-attempt index member are written, carrying the
          killed run's copied ``delivery`` address, ``run_delivery_id`` and subject descriptor, so
          the platform's FAILED delivery survives the prune and a crash-redelivery.

        ``act_on`` is the door's precondition — the resolved target states the kill may act on. The
        default (:data:`KILL_ACT_ON_ANY`) tears down a pending OR an already-resolved
        (answered/running) target, the whole-chain-walk behavior. A single-park door passes
        :data:`KILL_ACT_ON_PENDING`: when the WATCHed status shows the park was resolved in the claim
        window (an answer committed after the door read it pending), the resolved state is excluded,
        the MULTI writes NOTHING, and ``"skipped"`` is returned — so the answer's own continuation
        owns the run and the door delivers no FAILED, tears nothing down and clears no due record.

        The caller passes the run's ``delivery``/``run_delivery_id``/``subjects`` it read off the
        surviving record (state hash or continuation-due), and the kill-due TTL, first-attempt score
        and hard ``deadline_ms`` (the reaper's give-up horizon).
        """
        state_key = self.state_key(interaction_id)
        due_key = self.continuation_due_key(interaction_id)
        kill_key = self.kill_due_key(interaction_id)
        watch_keys = [state_key, due_key] + ([self.count_key(group_id)] if group_id is not None else [])
        kill_mapping = records._kill_due_mapping(
            reason,
            deadline_ms,
            json.dumps(delivery) if delivery is not None else None,
            run_delivery_id,
            json.dumps(subjects) if subjects is not None else None,
        )

        async with r.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(*watch_keys)
                    status = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "status")))
                    due_exists = bool(await cast("Awaitable[dict[str | bytes, str | bytes]]", pipe.hgetall(due_key)))
                    if status is None and not due_exists:
                        # Nothing live to kill: no park state and no running-entry due record.
                        await pipe.reset()
                        return "gone"
                    # The target's resolved state: pending (a live park) or answered (an
                    # answered/running entry, its state retained or aged to its due record alone).
                    resolved = "pending" if status == "pending" else "answered"
                    if resolved not in act_on:
                        # The door may act only on a still-pending target and the park was resolved in
                        # the claim window: write nothing, so the answer's own continuation owns the run.
                        await pipe.reset()
                        return "skipped"
                    prune = (
                        await self._read_kill_prune_fields(pipe, state_key, group_id) if status == "pending" else None
                    )
                    pipe.multi()
                    if prune is not None:
                        self._queue_kill_prune(pipe, interaction_id, group_id, prune, reason)
                    # UNCONDITIONAL of the pending status: a buffered/answered sibling's live due
                    # record is cleared so nothing redelivers into a torn-down run.
                    pipe.delete(due_key)
                    pipe.zrem(self.continuation_due_index_key, interaction_id)
                    pipe.hset(kill_key, mapping=kill_mapping)
                    pipe.expire(kill_key, kill_due_ttl)
                    pipe.zadd(self.kill_due_index_key, {interaction_id: first_attempt_at_ms})
                    await pipe.execute()
                except WatchError:
                    continue
                else:
                    if status == "answered":
                        return "answered"
                    # A pending park was pruned, or a running entry whose state hash aged out
                    # (``status`` is None — reached only WITH a continuation-due record present, the
                    # ``status is None and not due_exists`` case returns ``"gone"`` before the MULTI)
                    # had that due record cleared and its kill-due written: either way the entry is
                    # now torn down, so the caller delivers the FAILED and clears the kill-due at once.
                    return "pruned"

    async def _read_kill_prune_fields(self, pipe: Any, state_key: str, group_id: str | None) -> _KillPrune:
        """Read the denormalized fields a pending park's kill-prune MULTI writes from (pre-MULTI phase)."""
        audience = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "audience")))
        media_ids_field = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "media_ids")))
        media_ids = media_ids_field.split(",") if media_ids_field else []
        park_thread_id = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "thread_id")))
        to = self._addressed_to(serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "to"))))
        subjects = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "subjects")))
        remaining: int | None = None
        if group_id is not None:
            current = await pipe.get(self.count_key(group_id))
            if current is None:
                raise RuntimeError(f"pending count missing for group {group_id!r} with a live state at {state_key!r}")
            remaining = int(current) - 1
        return _KillPrune(
            to=to,
            audience=audience,
            media_ids=media_ids,
            park_thread_id=park_thread_id,
            subjects=subjects,
            remaining=remaining,
        )

    def _queue_kill_prune(
        self, pipe: Any, interaction_id: str, group_id: str | None, prune: _KillPrune, reason: str
    ) -> None:
        """Enqueue a pending park's prune writes into the kill MULTI — the same writes ``prune_pending`` makes."""
        pipe.delete(self.state_key(interaction_id))
        pipe.zrem(self._open_key_for(prune.to), interaction_id)
        pipe.zrem(self.pending_expiry_key, interaction_id)
        if prune.park_thread_id is not None:
            pipe.srem(self.thread_parks_key(prune.park_thread_id), interaction_id)
        self._queue_subject_leave(pipe, prune.subjects, interaction_id)
        if prune.media_ids and group_id is not None:
            pipe.srem(self.media_index_key(group_id), *prune.media_ids)
        if group_id is not None:
            pipe.decr(self.count_key(group_id))
            if prune.remaining is not None and prune.remaining <= 0:
                pipe.zrem(self.pending_key, group_id)
                pipe.zrem(self.pending_deadline_key, group_id)
                pipe.delete(self.count_key(group_id))
        pipe.xadd(
            self.events_key,
            cast(
                "dict[Any, Any]",
                events._event_fields(
                    events.REMOVED_EVENT, interaction_id, group_id or "", prune.audience, reason=reason
                ),
            ),
            maxlen=events._EVENTS_MAXLEN,
            approximate=True,
        )

    async def clear_kill_due(self, r: Redis, interaction_id: str) -> None:
        """Delete the kill-due record + drop its index member once the teardown and FAILED delivery committed.

        Atomic, and a no-op on an already-cleared record (a redelivery the original kill raced to
        completion), so double-clear is harmless.
        """
        pipe = r.pipeline()
        pipe.delete(self.kill_due_key(interaction_id))
        pipe.zrem(self.kill_due_index_key, interaction_id)
        await pipe.execute()

    async def thread_park_members(self, r: Redis, thread_id: str) -> list[str]:
        """The interaction ids of the async parks bound to ``thread_id`` — the thread reverse-index snapshot.

        The whole-chain kill's thread reach reads this so it can route each member through the kill
        seam. A missing/drained set is an empty list.
        """
        key = self.thread_parks_key(thread_id)
        return [serde.as_str(member) for member in await cast("Awaitable[set[str | bytes]]", r.smembers(key))]

    async def reconcile_thread_park_members(self, r: Redis, thread_id: str, members: list[str]) -> None:
        """Drop the snapshotted ``members`` from ``thread_id``'s reverse index — the kill cascade's cleanup.

        SREM only the members the cascade snapshotted (NOT a blind DELETE): a park added to the
        thread CONCURRENTLY with the cascade keeps its own member and stays cascade-cancellable on a
        retry. The kill seam's prune already dropped a pruned park's member (repeating is a no-op);
        this additionally reconciles an orphan member whose state had already vanished. A no-op on an
        empty snapshot.
        """
        if members:
            await cast("Awaitable[int]", r.srem(self.thread_parks_key(thread_id), *members))

    async def add_outcome(
        self,
        r: Redis,
        *,
        completion_id: str,
        interaction_id: str,
        status: Literal["finished", "failed"],
        result: Any,
        candidates: SubjectCandidates,
        retention_ttl: int,
        run_delivery_id: str | None = None,
    ) -> None:
        """Park a resumed run's terminal for its subject — the waiting-outcome the subject owner later takes.

        Written when a resumed run reaches a terminal with a subject but no live receiver and no
        address. Keyed by the per-run ``completion_id``, so a redelivery of the same run — or
        a buffered sibling re-driving the SAME terminal — is a no-op: exactly one row per run
        terminal (a second write of the same terminal is a no-op). The row JOINS the subject index under each of
        the run's subject keys (member = ``completion_id``) so the subject owner lists and takes it,
        JOINS the retention index (member = ``completion_id``, score = its creation time) so the
        retention sweep can find it aged without a SCAN, and takes one ``open_caller`` slot — COUNTED
        under ``max_concurrent_caller`` but NEVER refused here, since a finished run's result must not
        be lost (only a fresh caller ask is refused at a full cap). The resumed interaction itself,
        whose ``running`` membership is now complete, is dropped from the same subject keys in the
        SAME step, so the entry moves cleanly ``running`` → ``finished``/``failed``. Its open slot was
        already freed by ``record_answer``. Idempotent by construction: a re-run repeats identical
        writes.

        ``retention_ttl`` is the record's Redis TTL — a BACKSTOP set to a MULTIPLE of the retention
        horizon (the sweep drops the untaken outcome at one horizon, before this backstop deletes it),
        so an aged outcome always leaves through the sweep's loud event, never a silent TTL delete.
        ``run_delivery_id`` rides the row (with ``interaction_id``) so the sweep's dropped-untaken
        event names the run.
        """
        descriptor = records.subjects_descriptor(candidates)
        subject_keys = records.iter_subject_keys(descriptor)
        existing = await cast("Awaitable[dict[str | bytes, str | bytes]]", r.hgetall(self.outcome_key(completion_id)))
        pipe = r.pipeline()
        for target_kind, target_name, kind, key in subject_keys:
            # The resumed interaction's ``running`` membership is complete — drop it.
            pipe.srem(self.subject_parks_key(target_kind, target_name, kind, key), interaction_id)
        if not existing:
            now_ms = ttl._now_ms()
            outcome_key = self.outcome_key(completion_id)
            pipe.hset(
                outcome_key,
                mapping=records._outcome_mapping(
                    completion_id, status, result, descriptor, now_ms, interaction_id, run_delivery_id
                ),
            )
            pipe.expire(outcome_key, retention_ttl)
            # The retention sweep's work index: scored by creation time so the sweep finds an aged
            # outcome without a SCAN and drops it before the Redis TTL backstop can silently delete it.
            pipe.zadd(self.outcome_retention_index_key, {completion_id: now_ms})
            # The outcome counts under the caller cap; its purge deadline is the
            # retention horizon, so the open-index stale-purge never drops a live one.
            pipe.zadd(self.open_caller_key, {completion_id: now_ms + retention_ttl * 1000})
            for target_kind, target_name, kind, key in subject_keys:
                parks_key = self.subject_parks_key(target_kind, target_name, kind, key)
                scopes_key = self.subject_scopes_key(kind, key)
                pipe.sadd(parks_key, completion_id)
                pipe.sadd(scopes_key, f"{target_kind}:{target_name}")
                # Set-or-extend the index sets to the retention horizon (``NX`` sets it
                # when the set is fresh here, ``GT`` raises it past the park's own).
                pipe.expire(parks_key, retention_ttl, nx=True)
                pipe.expire(parks_key, retention_ttl, gt=True)
                pipe.expire(scopes_key, retention_ttl, nx=True)
                pipe.expire(scopes_key, retention_ttl, gt=True)
        await pipe.execute()

    async def claim_outcome(self, r: Redis, completion_id: str) -> WaitingOutcome | None:
        """Atomically TAKE a waiting outcome: read it, delete the row, and drop it from every index.

        The subject owner's take. Returns the :class:`WaitingOutcome` when this call claimed it, or
        ``None`` when it was already taken / never existed (a lost duplicate race). The read + delete
        + index removals run in one WATCH/MULTI transaction, so two racing takers never both claim
        the same row.
        """
        outcome_key = self.outcome_key(completion_id)
        async with r.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(outcome_key)
                    raw = await cast("Awaitable[dict[str | bytes, str | bytes]]", pipe.hgetall(outcome_key))
                    if not raw:
                        await pipe.reset()
                        return None
                    fields = {serde.as_str(k): serde.as_str(v) for k, v in raw.items()}
                    outcome = self._outcome_from_fields(fields)
                    pipe.multi()
                    self._queue_outcome_removal(pipe, completion_id, fields)
                    await pipe.execute()
                except WatchError:
                    continue
                else:
                    return outcome

    async def reconcile_orphan_outcome(self, r: Redis, completion_id: str) -> bool:
        """Drop the lingering index members of an outcome whose row left through its Redis TTL backstop.

        The retention index carries no TTL, so when a row is deleted by its Redis TTL backstop
        (rather than a take or a sweep, both of which drop the index member in the same step), the
        member is orphaned and the retention sweep re-reads it every pass. This removes it — from the
        retention index and the caller open-index (the subject-parks sets carry their own TTL and
        need the now-gone row's descriptor, so they self-expire) — in ONE atomic step and returns
        ``True`` only for the caller that actually removed the retention member. A racing take that
        already cleared it, or a concurrent sweep, removes nothing and gets ``False``, so the loud
        drop path downstream fires exactly once.
        """
        async with r.pipeline() as pipe:
            pipe.zrem(self.outcome_retention_index_key, completion_id)
            pipe.zrem(self.open_caller_key, completion_id)
            removed_retention, _ = await pipe.execute()
        return bool(removed_retention)

    async def delete_outcome(self, r: Redis, completion_id: str) -> None:
        """Drop a waiting outcome unconditionally — the kill / erase / retention-sweep cleanup.

        Reads the row for its subject descriptor, then deletes it and drops it from every subject
        index and the caller open-index. A no-op on an already-gone row, so double-delete is
        harmless.
        """
        outcome_key = self.outcome_key(completion_id)
        raw = await cast("Awaitable[dict[str | bytes, str | bytes]]", r.hgetall(outcome_key))
        if not raw:
            return
        fields = {serde.as_str(k): serde.as_str(v) for k, v in raw.items()}
        pipe = r.pipeline()
        self._queue_outcome_removal(pipe, completion_id, fields)
        await pipe.execute()

    def _queue_outcome_removal(self, pipe: Any, completion_id: str, fields: dict[str, str]) -> None:
        """Enqueue the row delete + every index removal for one waiting outcome."""
        pipe.delete(self.outcome_key(completion_id))
        pipe.zrem(self.open_caller_key, completion_id)
        pipe.zrem(self.outcome_retention_index_key, completion_id)
        subjects_field = fields.get("subjects")
        if subjects_field is not None:
            for target_kind, target_name, kind, key in records.iter_subject_keys(json.loads(subjects_field)):
                pipe.srem(self.subject_parks_key(target_kind, target_name, kind, key), completion_id)

    @staticmethod
    def _outcome_from_fields(fields: dict[str, str]) -> WaitingOutcome:
        """Build a :class:`WaitingOutcome` from a raw outcome-hash mapping."""
        status = fields["status"]
        if status not in ("finished", "failed"):
            raise RuntimeError(f"waiting outcome {fields.get('completion_id')!r} carries invalid status {status!r}")
        subjects_field = fields.get("subjects")
        return WaitingOutcome(
            completion_id=fields["completion_id"],
            status=cast("Literal['finished', 'failed']", status),
            result=json.loads(fields["result"]),
            subjects=json.loads(subjects_field) if subjects_field is not None else None,
            interaction_id=fields.get("interaction_id"),
            run_delivery_id=fields.get("run_delivery_id"),
        )

    async def rekey_subject(self, r: Redis, *, kind: str, old_key: str, new_key: str) -> list[str]:
        """Re-key every entry addressed to ``(kind, old_key)`` onto ``(kind, new_key)`` — the person merge.

        A person merge folds one subject key (the absorbed person id, and the person-thread thread
        key) into another. This moves, across EVERY scope that used ``old_key`` (found through the
        ``subject-scopes`` index, no SCAN), each entry's membership from the old subject-parks set
        to the new one, and rewrites every denormalized subject copy of the entry — its state hash
        (``subjects``, ``request``, ``continuation_state_context``), its durable continuation-due
        record (``state_context``) and kill-due record (``subjects``) the detached redelivery legs
        read, and its outcome hash (``subjects``) — so a later re-park, a continuation/kill
        redelivery and any subsequent index-leave all address the NEW key. The old scopes set is
        folded into the new one. Returns the ids moved. Idempotent: re-running finds the old sets
        drained.
        """
        if old_key == new_key:
            return []
        scopes_key = self.subject_scopes_key(kind, old_key)
        scope_tokens = [
            serde.as_str(token) for token in await cast("Awaitable[set[str | bytes]]", r.smembers(scopes_key))
        ]
        moved: list[str] = []
        for token in scope_tokens:
            scope_kind, _, scope_name = token.partition(":")
            scope_target_kind = cast("ConversationTargetKind", scope_kind)
            old_parks = self.subject_parks_key(scope_target_kind, scope_name, kind, old_key)
            members = [serde.as_str(m) for m in await cast("Awaitable[set[str | bytes]]", r.smembers(old_parks))]
            for member in members:
                await self._rekey_member(r, member, kind, old_key, new_key)
                moved.append(member)
            new_parks = self.subject_parks_key(scope_target_kind, scope_name, kind, new_key)
            pipe = r.pipeline()
            if members:
                pipe.sadd(new_parks, *members)
                pipe.srem(old_parks, *members)
            pipe.sadd(self.subject_scopes_key(kind, new_key), token)
            pipe.srem(scopes_key, token)
            await pipe.execute()
        return moved

    async def _rekey_member(self, r: Redis, member: str, kind: str, old_key: str, new_key: str) -> None:
        """Rewrite every stored subject copy of one moved entry so ``by_kind[kind]`` points at ``new_key``.

        A moved member's subject descriptor is denormalized in more than one durable place, each read
        by a different leg after the merge — so all are re-keyed in the same walk:

        * the state hash (a park / running entry: ``subjects``, ``request``, ``continuation_state_context``);
        * the continuation-due record's ``state_context`` copy, which a detached redelivery reads to
          subject-track the resumed run's outcome;
        * the kill-due record's ``subjects`` copy, which a crash-redelivery reads to subject-track the
          killed run's FAILED;
        * the outcome hash (a waiting outcome: ``subjects``).

        Each is a guarded read-modify-write, no-op when the record is absent or already names the new
        key, so a re-park, a continuation redelivery, a kill redelivery and an index-leave after the
        merge all address the survivor's key.
        """
        await self._rekey_state_hash(r, member, kind, old_key, new_key)
        await self._rekey_continuation_due(r, member, kind, old_key, new_key)
        await self._rekey_kill_due(r, member, kind, old_key, new_key)
        await self._rekey_outcome_hash(r, member, kind, old_key, new_key)

    async def _rekey_state_hash(self, r: Redis, member: str, kind: str, old_key: str, new_key: str) -> None:
        """Re-key the state hash's ``subjects`` descriptor, ``request`` and ``continuation_state_context``."""
        state_key = self.state_key(member)
        raw_state = await cast("Awaitable[dict[str | bytes, str | bytes]]", r.hgetall(state_key))
        if not raw_state:
            return
        fields = {serde.as_str(k): serde.as_str(v) for k, v in raw_state.items()}
        updates: dict[str, str] = {}
        subjects_field = fields.get("subjects")
        if subjects_field is not None:
            descriptor = json.loads(subjects_field)
            if descriptor["by_kind"].get(kind) == old_key:
                descriptor["by_kind"][kind] = new_key
                updates["subjects"] = json.dumps(descriptor)
        request = InteractionRequest.model_validate_json(fields["request"])
        new_ctx = self._rekey_context(request.continuation_state_context, kind, old_key, new_key)
        if new_ctx is not None:
            updates["request"] = request.model_copy(update={"continuation_state_context": new_ctx}).model_dump_json()
            updates["continuation_state_context"] = new_ctx.model_dump_json()
        if updates:
            await cast("Awaitable[int]", r.hset(state_key, mapping=updates))

    async def _rekey_continuation_due(self, r: Redis, member: str, kind: str, old_key: str, new_key: str) -> None:
        """Re-key the durable continuation-due record's ``state_context`` copy the redelivery leg reads."""
        due_key = self.continuation_due_key(member)
        raw = await cast("Awaitable[dict[str | bytes, str | bytes]]", r.hgetall(due_key))
        if not raw:
            return
        fields = {serde.as_str(k): serde.as_str(v) for k, v in raw.items()}
        context_field = fields.get("state_context")
        if context_field is None:
            return
        new_ctx = self._rekey_context(StateContext.model_validate_json(context_field), kind, old_key, new_key)
        if new_ctx is not None:
            await cast("Awaitable[int]", r.hset(due_key, mapping={"state_context": new_ctx.model_dump_json()}))

    async def _rekey_kill_due(self, r: Redis, member: str, kind: str, old_key: str, new_key: str) -> None:
        """Re-key the durable kill-due record's ``subjects`` copy the crash-redelivery leg reads."""
        kill_key = self.kill_due_key(member)
        raw = await cast("Awaitable[dict[str | bytes, str | bytes]]", r.hgetall(kill_key))
        if not raw:
            return
        fields = {serde.as_str(k): serde.as_str(v) for k, v in raw.items()}
        subjects_field = fields.get("subjects")
        if subjects_field is None:
            return
        descriptor = json.loads(subjects_field)
        if descriptor["by_kind"].get(kind) == old_key:
            descriptor["by_kind"][kind] = new_key
            await cast("Awaitable[int]", r.hset(kill_key, mapping={"subjects": json.dumps(descriptor)}))

    async def _rekey_outcome_hash(self, r: Redis, member: str, kind: str, old_key: str, new_key: str) -> None:
        """Re-key a waiting-outcome hash's ``subjects`` descriptor."""
        outcome_key = self.outcome_key(member)
        raw_outcome = await cast("Awaitable[dict[str | bytes, str | bytes]]", r.hgetall(outcome_key))
        if not raw_outcome:
            return
        fields = {serde.as_str(k): serde.as_str(v) for k, v in raw_outcome.items()}
        subjects_field = fields.get("subjects")
        if subjects_field is None:
            return
        descriptor = json.loads(subjects_field)
        if descriptor["by_kind"].get(kind) == old_key:
            descriptor["by_kind"][kind] = new_key
            await cast("Awaitable[int]", r.hset(outcome_key, mapping={"subjects": json.dumps(descriptor)}))

    @staticmethod
    def _rekey_context(context: StateContext | None, kind: str, old_key: str, new_key: str) -> StateContext | None:
        """Return ``context`` with ``candidates.by_kind[kind]`` re-keyed to ``new_key``, or ``None`` when unaffected."""
        if context is None or context.candidates.by_kind.get(kind) != old_key:
            return None
        new_by_kind = dict(context.candidates.by_kind)
        new_by_kind[kind] = new_key
        new_candidates = context.candidates.model_copy(update={"by_kind": new_by_kind})
        return context.model_copy(update={"candidates": new_candidates})
