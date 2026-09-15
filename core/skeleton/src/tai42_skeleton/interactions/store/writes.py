"""The durable question-lifecycle mutations.

Persist a new question, atomically reserve an open slot, claim-and-record an answer, prune an
abandoned question, and cascade-cancel a thread's parks.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Literal, cast

from redis.asyncio import Redis
from redis.exceptions import WatchError
from tai42_contract.interactions import InteractionRequest, InteractionResponse, InteractionState

from . import events, records, scripts, serde, ttl
from .keys import _StoreKeys

# The three end states ``prune_pending`` distinguishes: ``"pruned"`` deleted a
# still-pending question; ``"answered"`` found an answered status (no writes);
# ``"gone"`` found no state key at all (missing/expired, no writes).
PruneResult = Literal["pruned", "answered", "gone"]


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
    continuation_tool: str | None
    continuation_identity: str | None
    continuation_fingerprint: str | None
    continuation_state_context: str | None
    remaining: int


class _StoreWrites(_StoreKeys):
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
        state_mapping = self._build_state_mapping(request, continuation_fingerprint, thread_id)
        new_media_ids = ttl._media_ids_of(request)
        pipe = r.pipeline()
        thread_parks_key = self._queue_record_writes(
            pipe, request, state_mapping, ticket, ticket_ttl, open_member_reserved
        )
        self._queue_ttls(pipe, request, idle_ttl, expiry_ttl_margin_seconds, thread_parks_key, new_media_ids)
        await pipe.execute()

    def _build_state_mapping(
        self, request: InteractionRequest, continuation_fingerprint: str | None, thread_id: str | None
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
        return state_mapping

    def _queue_record_writes(
        self,
        pipe: Any,
        request: InteractionRequest,
        state_mapping: dict[str, str],
        ticket: str | None,
        ticket_ttl: int | None,
        open_member_reserved: bool,
    ) -> str | None:
        """Enqueue the record + index writes; return the ``thread_parks_key`` (or ``None``) for the TTL step.

        Writes the stream, state, count, pending + deadline indexes, expiry member, thread-park
        member, open member, ticket, and add-event.
        """
        group_key = self.group_key(request.group_id)
        state_key = self.state_key(request.interaction_id)
        count_key = self.count_key(request.group_id)
        # ``ZREMRANGEBYSCORE(open_key, 0, now_ms)`` runs UNCONDITIONALLY on every call,
        # purging open-index members whose deadline has passed — a SIGKILLed waiter's
        # member would otherwise linger forever (the ZSET has no TTL).
        pipe.zremrangebyscore(self.open_key, 0, ttl._now_ms())
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
        if not open_member_reserved:
            # The atomic guard already added this member; re-adding here would
            # double-count the open index.
            pipe.zadd(self.open_key, {request.interaction_id: ttl._timeout_ms(request)})
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
        awaitable at runtime.
        """
        reserved = await cast(
            "Awaitable[int]",
            r.eval(
                scripts._OPEN_RESERVE_LUA,
                1,
                self.open_key,
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
        pipe.zrem(self.open_key, interaction_id)
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
                    pipe.zrem(self.open_key, interaction_id)
                    # Drop any async-expiry member alongside the state (a no-op for a
                    # sync question, never a member).
                    pipe.zrem(self.pending_expiry_key, interaction_id)
                    if park_thread_id is not None:
                        # Drop this park from its thread's reverse index in the SAME
                        # MULTI — the cascade a thread delete drives runs through here,
                        # so the index self-heals as each park is pruned.
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

    async def cancel_thread_parks(self, r: Redis, thread_id: str) -> list[str]:
        """Cancel every async park bound to ``thread_id`` — the ONE cascade a thread delete fires.

        Fired by a conversation thread delete (admin-delete, forget-me, route-delete) so a
        parked ``ask_user`` the deletion would ORPHAN is torn down instead of lingering (its
        expiry reaper later firing a continuation into a thread that no longer exists → a
        delivery retry storm, its channel correlation muting the participant's number until the
        ~24h deadline).

        Reads the thread's reverse-index members and runs the EXISTING ``prune_pending`` for
        each: status-gated and idempotent, it removes a still-pending park WITHOUT firing any
        continuation — deliberately not ``record_answer`` (which would enqueue a dead
        completion) — and is a clean no-op on a park already answered or gone. A member whose
        state already vanished is skipped (nothing to prune); the snapshotted members are then
        SREM'd (NOT a blind key delete) so such an orphan member is reconciled off while a park
        added to the thread concurrently with the cascade keeps its member and stays cancellable
        on a retry. Returns the interaction ids read from the index.

        Idempotent: cancelling a thread with no parks (a missing set) is a no-op, and
        cancelling twice finds the set drained/absent the second time. The recovery is proven:
        once the interaction state is gone the answer-door returns not-found, and each channel
        bridges the next reply as a fresh turn and self-releases its correlation — so this
        cancellation is channel-blind and enumerates no channels.
        """
        key = self.thread_parks_key(thread_id)
        members = [serde.as_str(member) for member in await cast("Awaitable[set[str | bytes]]", r.smembers(key))]
        for interaction_id in members:
            group_id = serde.as_str(
                await cast("Awaitable[str | bytes | None]", r.hget(self.state_key(interaction_id), "group_id"))
            )
            if group_id is None:
                # The park's state already vanished (answered/expired/pruned): nothing to
                # prune, and the trailing snapshot SREM reconciles the orphan member off.
                continue
            await self.prune_pending(r, interaction_id, group_id)
        # Remove ONLY the members we snapshotted (SREM, not a blind DELETE of the key): a park
        # added to this thread CONCURRENTLY with the cascade — between the smembers snapshot
        # above and here — keeps its own index member, so it stays cascade-cancellable on a
        # retry instead of being silently orphaned by wiping the whole set. prune_pending
        # already SREM'd each park it pruned (repeating is a no-op); this additionally
        # reconciles the snapshot's orphan members. The set auto-deletes once its last member
        # is removed; its TTL backstops either way.
        if members:
            await cast("Awaitable[int]", r.srem(key, *members))
        return members
