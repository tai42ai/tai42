"""The read/query/audit operations plus the reaper's due-record claims: every path
that inspects the store without mutating the question lifecycle (the pending audit,
the rename referee's scans, the expiry/continuation due lists and retry claim)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from redis.asyncio import Redis
from tai42_contract.interactions import InteractionRequest, InteractionResponse, InteractionState
from tai42_contract.states import StateContext

from . import scripts, serde, ttl
from .keys import _StoreKeys
from .records import CONTINUATION_DROPPED, ContinuationDue, ContinuationRetryDrop

if TYPE_CHECKING:
    from .writes import PruneResult

# The read-only ``list_pending`` admin audit truncates each question to this many
# characters so one over-long question can never bloat the audit frame; the full
# text stays on the durable record (this preview is display-only).
_PENDING_QUESTION_PREVIEW_CHARS = 200

# The default cap on ``list_pending`` — the audit returns at most this many of the
# soonest-to-expire parks when a caller names no explicit limit.
_PENDING_LIST_DEFAULT_LIMIT = 500


class _StoreReads(_StoreKeys):
    if TYPE_CHECKING:
        # Provided by ``_StoreWrites`` in the composed ``InteractionStore``; ``pending``
        # reconciles an abandoned question through it.
        async def prune_pending(
            self, r: Redis, interaction_id: str, group_id: str, *, reason: str | None = None
        ) -> PruneResult: ...

    async def resolve_ticket(self, r: Redis, ticket: str) -> str | None:
        """Return the interaction id a callback ticket maps to, or ``None`` when
        the ticket never existed or has expired (lookup-by-exact-key IS the
        comparison — no user-supplied string is compared in Python)."""
        return serde.as_str(await cast("Awaitable[str | bytes | None]", r.get(self.ticket_key(ticket))))

    async def due_expiries(self, r: Redis, now: datetime) -> list[str]:
        """The interaction ids of async parks whose ``expiry_at`` is at or before
        ``now`` — the expiry reaper's work list. Reads the per-interaction expiry
        index by score; a member is removed from it only when the question leaves
        pending (answer/expiry/prune), so a lingering member for a
        vanished/answered state is re-reconciled by the reaper (claim returns
        no-op, the member is dropped)."""
        cutoff = int(now.timestamp() * 1000)
        raw = await r.zrangebyscore(self.pending_expiry_key, 0, cutoff)
        return [serde.as_str(member) for member in raw]

    async def drop_expiry_member(self, r: Redis, interaction_id: str) -> None:
        """Remove ``interaction_id`` from the expiry index — the reaper's cleanup for
        a member whose state already vanished/answered (so the claim was a no-op),
        keeping the index from re-listing a dead member every pass."""
        await r.zrem(self.pending_expiry_key, interaction_id)

    async def list_pending(
        self, r: Redis, *, now: datetime, limit: int = _PENDING_LIST_DEFAULT_LIMIT
    ) -> list[dict[str, Any]]:
        """A read-only admin audit of the currently-parked async asks — every member
        of the per-interaction expiry index (``pending:expiry``), soonest deadline
        first, as a flat per-item mapping a watchdog flow can inspect natively.

        Reads the index by rank WITHOUT mutating it — no claim, no TTL change, no drop
        of a stale member (reconciling a vanished member stays the reaper's job) — so
        an audit never perturbs the park lifecycle. The lowest-scored (soonest to
        expire) ``limit`` members are returned; ``limit`` defaults to
        :data:`_PENDING_LIST_DEFAULT_LIMIT`. ``now`` is accepted for call-site symmetry
        with :meth:`due_expiries` (the audit lists ALL parks, due or not, so it does
        not filter on it). For each id the state hash is fetched; an id whose state has
        vanished (answered/expired/pruned between the index read and the hash read) is
        skipped, so a listed item always has live state.

        Per item: ``interaction_id``, ``group_id``, ``question`` (truncated to
        :data:`_PENDING_QUESTION_PREVIEW_CHARS`), ``channel``, ``recipient``,
        ``audience``, ``thread_id`` (the denormalized hash field when a park carries
        one — absent-tolerant, ``None`` for a park raised before the cascade feature),
        ``expiry_at``, ``created_at``, ``mode``."""
        if limit <= 0:
            return []
        raw_ids = await r.zrange(self.pending_expiry_key, 0, limit - 1)
        items: list[dict[str, Any]] = []
        for raw_id in raw_ids:
            interaction_id = serde.as_str(raw_id)
            raw = await cast("Awaitable[dict[str | bytes, str | bytes]]", r.hgetall(self.state_key(interaction_id)))
            state = serde.state_from_raw(raw)
            if state is None or state.status != "pending":
                # Vanished, or an answered member lingering in the index before the
                # reaper reconciles it off: the index member is left untouched (that
                # stays the reaper's job) and the item is skipped, so a listed park is
                # always live and still awaiting an answer.
                continue
            request = state.request
            # ``thread_id`` is a denormalized hash field the cascade feature stamps; it
            # is absent on older parks, so read it tolerantly from the raw hash rather
            # than the parsed request (which never carried it). Normalize the key across
            # a decode/no-decode redis client.
            fields = {serde.as_str(k): serde.as_str(v) for k, v in raw.items()}
            question = request.question
            if len(question) > _PENDING_QUESTION_PREVIEW_CHARS:
                question = question[:_PENDING_QUESTION_PREVIEW_CHARS]
            items.append(
                {
                    "interaction_id": request.interaction_id,
                    "group_id": state.group_id,
                    "question": question,
                    "channel": request.channel,
                    "recipient": request.recipient,
                    "audience": request.audience,
                    "thread_id": fields.get("thread_id"),
                    "expiry_at": request.expiry_at.isoformat() if request.expiry_at is not None else None,
                    "created_at": request.created_at.isoformat(),
                    "mode": request.mode,
                }
            )
        return items

    async def parked_continuation_tools(self, r: Redis) -> list[str]:
        """Every currently-parked async interaction's ``continuation_tool``, read by
        walking the FULL per-interaction expiry index (``pending:expiry``, member =
        interaction id) — one hash read per member, uncapped.

        Distinct from :meth:`list_pending` on purpose: that audit is bounded by
        ``limit`` (default :data:`_PENDING_LIST_DEFAULT_LIMIT`) and its item view omits
        ``continuation_tool`` entirely, so it cannot be the rename referee's source — a
        capped scan would silently miss a park and let a stranding rename through. The
        referee needs EVERY park's resume target, so this reads the whole index. A member
        whose state vanished/answered between the index read and the hash read (or a sync
        question wrongly indexed) carries no ``continuation_tool`` and is skipped."""
        tools: list[str] = []
        for raw_id in await r.zrange(self.pending_expiry_key, 0, -1):
            interaction_id = serde.as_str(raw_id)
            tool = serde.as_str(
                await cast(
                    "Awaitable[str | bytes | None]",
                    r.hget(self.state_key(interaction_id), "continuation_tool"),
                )
            )
            if tool is not None:
                tools.append(tool)
        return tools

    async def continuation_due_tools(self, r: Redis) -> list[str]:
        """Every answered/expired-but-not-yet-redelivered park's continuation ``tool``,
        read by walking the FULL continuation-due index (``continuation:due``, member =
        interaction id) — one hash read per member, uncapped.

        The companion to :meth:`parked_continuation_tools` for the rename referee: an
        OPEN park lives in ``pending:expiry`` and resolves OUT of it on answer/expiry
        (``record_answer`` zrem), which durably enqueues the continuation-due record the
        reaper re-fires as ``run_tool(<tool>)``. That tool is a live holder THIS index —
        not the pending one — carries, so a rename between answer and redelivery would
        strand it unless both indices are unioned. A member whose record hash TTL-expired
        between the index read and the hash read is a genuinely-gone record and is
        skipped; a PRESENT record missing its ``tool`` field is torn and raises loudly,
        never a silent drop."""
        tools: list[str] = []
        for raw_id in await r.zrange(self.continuation_due_index_key, 0, -1):
            interaction_id = serde.as_str(raw_id)
            raw = await cast(
                "Awaitable[dict[str | bytes, str | bytes]]",
                r.hgetall(self.continuation_due_key(interaction_id)),
            )
            if not raw:
                continue
            fields = {serde.as_str(k): serde.as_str(v) for k, v in raw.items()}
            tool = fields.get("tool")
            if tool is None:
                raise RuntimeError(f"continuation-due record {interaction_id!r} present but carries no 'tool' field")
            tools.append(tool)
        return tools

    async def continuation_fingerprint(self, r: Redis, interaction_id: str) -> str | None:
        """The async fire's captured key fingerprint stashed on the state hash
        (``""`` for a gate-off fire), or ``None`` when the question carries none (a
        sync question, or a state that has expired). The answer/expiry path passes
        it to the execution-identity bind that runs the stored continuation."""
        raw = await cast(
            "Awaitable[str | bytes | None]", r.hget(self.state_key(interaction_id), "continuation_fingerprint")
        )
        return serde.as_str(raw)

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
        return [serde.as_str(member) for member in raw]

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
                scripts._CONTINUATION_RETRY_CLAIM_LUA,
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
        fields = {serde.as_str(k): serde.as_str(v) for k, v in zip(it, it, strict=True)}
        raw_context = fields.get("state_context")
        return ContinuationDue(
            interaction_id=interaction_id,
            tool=fields["tool"],
            identity=fields["identity"],
            fingerprint=fields["fingerprint"],
            answer=json.loads(fields["answer"]),
            attempts=int(fields["attempts"]),
            state_context=StateContext.model_validate_json(raw_context) if raw_context is not None else None,
        )

    async def count_open(self, r: Redis) -> int:
        """The live open-question count: purge open-index members whose deadline
        has passed, then ``ZCARD``. All ``open_key`` access stays inside the store
        so no caller touches the key inline. The ``max_concurrent`` cap enforces
        itself atomically in ``reserve_open_slot``; this read serves callers that
        only need the current count."""
        await r.zremrangebyscore(self.open_key, 0, ttl._now_ms())
        return await cast("Awaitable[int]", r.zcard(self.open_key))

    async def get_state(self, r: Redis, interaction_id: str) -> InteractionState | None:
        # redis-py's async stubs type ``hgetall`` with the sync (non-awaitable)
        # return; it is awaitable at runtime.
        raw = await cast("Awaitable[dict[str | bytes, str | bytes]]", r.hgetall(self.state_key(interaction_id)))
        return serde.state_from_raw(raw)

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
            group_id = serde.as_str(raw_group)
            entries = await r.xrange(self.group_key(group_id))
            if not entries:
                # The group's stream expired but lingered in the indexes — prune
                # the phantom from both so the badge/list don't count it.
                await r.zrem(self.pending_key, group_id)
                await r.zrem(self.pending_deadline_key, group_id)
                continue
            requests = [
                InteractionRequest.model_validate_json(serde.as_str(fields.get("request") or fields.get(b"request")))
                for _entry_id, fields in entries
            ]
            # One pipeline for the whole group's state hashes — no N+1.
            pipe = r.pipeline()
            for req in requests:
                pipe.hgetall(self.state_key(req.interaction_id))
            raw_states = await pipe.execute()
            for req, raw in zip(requests, raw_states, strict=True):
                state = serde.state_from_raw(raw)
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
        return InteractionResponse.model_validate_json(serde.as_str(value))
