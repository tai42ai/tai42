"""The atomic answer claim and the durable continuation-due outbox enqueue.

Claim-and-record an answer under WATCH/MULTI, and — for an async park — enqueue the durable,
flow-blind continuation-due record in the same transaction.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import WatchError
from tai42_contract.interactions import InteractionResponse

from . import events, records, serde
from .writes_base import AskTo, _StoreWritesBase


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


class _StoreAnswerWrites(_StoreWritesBase):
    """The atomic answer claim and durable continuation-due enqueue."""

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
