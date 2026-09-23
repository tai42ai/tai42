"""The durable question add, atomic open-slot reservation, and prune.

Persist a new question and refresh its TTLs, atomically reserve an open slot under the concurrency
cap, and prune an abandoned or timed-out question.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import Any, Literal, cast

from redis.asyncio import Redis
from redis.exceptions import WatchError
from tai42_contract.interactions import InteractionRequest, InteractionState

from . import events, records, scripts, serde, ttl
from .writes_base import AskTo, _StoreWritesBase

# The three end states ``prune_pending`` distinguishes: ``"pruned"`` deleted a
# still-pending question; ``"answered"`` found an answered status (no writes);
# ``"gone"`` found no state key at all (missing/expired, no writes).
PruneResult = Literal["pruned", "answered", "gone"]


class _StoreWrites(_StoreWritesBase):
    """The durable question add, atomic open-slot reservation, and prune."""

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
