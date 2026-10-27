"""The reaper's work lists and its due-record claims.

Each reaper leg reads its due members by score off one index (the expiry index, the
continuation-due index, the kill-due index, the waiting-outcome retention index) and claims a
member atomically before acting on it, so two racing reapers never both act on one record.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from datetime import datetime
from typing import Any, cast

from redis.asyncio import Redis
from tai42_contract.interactions import CallerAskLanding, DeliveryAddress
from tai42_contract.presets import PresetParkStages
from tai42_contract.states import StateBinding, StateContext

from . import scripts, serde
from .keys import _StoreKeys
from .records import (
    CONTINUATION_DROPPED,
    KILL_DROPPED,
    ContinuationDue,
    ContinuationRetryDrop,
    KillDue,
    KillRetryDrop,
    decode_chain_keys,
)


class _StoreReaperDues(_StoreKeys):
    """The reaper's due lists (expiries, continuations, kills, untaken outcomes) and their atomic claims."""

    async def due_expiries(self, r: Redis, now: datetime) -> list[str]:
        """The interaction ids of async parks whose ``expiry_at`` is at or before ``now``.

        The expiry reaper's work list. Reads the per-interaction expiry index by
        score; a member is removed from it in the same MULTI that ends its pending
        state (an answer claim, a kill prune, a cancel prune), so a listed member
        is either a pending park or one whose keys lapsed unresolved, which the
        reaper surfaces and reconciles through :meth:`drop_expiry_member`.
        """
        cutoff = int(now.timestamp() * 1000)
        raw = await r.zrangebyscore(self.pending_expiry_key, 0, cutoff)
        return [serde.as_str(member) for member in raw]

    async def drop_expiry_member(self, r: Redis, interaction_id: str) -> int | None:
        """Remove ``interaction_id`` from the expiry index; return its ``expiry_at`` in ms, or ``None``.

        The witness of a park that lapsed unresolved: every resolution removes the member with the
        state in one MULTI, so a member still indexed when its pending state is absent lapsed
        unresolved, and this call removes it and returns its score (the park's ``expiry_at``). It
        returns ``None`` when nothing was indexed, because a resolution already removed the member.
        The read and the removal run in one MULTI, so two reapers never both see the member.
        """
        pipe = r.pipeline()
        pipe.zscore(self.pending_expiry_key, interaction_id)
        pipe.zrem(self.pending_expiry_key, interaction_id)
        score, removed = await pipe.execute()
        if not removed:
            return None
        return int(score)

    async def due_continuations(self, r: Redis, now: datetime) -> list[str]:
        """The interaction ids of continuation-due records whose next-attempt time is at or before ``now``.

        The redelivery reaper's work list. A member survives until its
        continuation's ``run_tool`` returns (or its record TTL-expires and the
        retry-claim reconciles the orphan member off).
        """
        cutoff = int(now.timestamp() * 1000)
        raw = await r.zrangebyscore(self.continuation_due_index_key, 0, cutoff)
        return [serde.as_str(member) for member in raw]

    async def claim_continuation_retry(
        self, r: Redis, interaction_id: str, now: datetime, backoff_base_ms: int, backoff_cap_ms: int
    ) -> ContinuationDue | ContinuationRetryDrop | None:
        """Atomically claim a due continuation-due record for redelivery.

        Advance its next-attempt score by an exponential backoff (base doubled
        per prior attempt, capped) and return the record to re-fire. Returns
        ``None`` when the member is not (or no longer) due — already cleared or
        already re-claimed this window by a racing reaper. Returns
        ``CONTINUATION_DROPPED`` when the member is an orphan whose record hash
        TTL-expired past its retention horizon (which this reconciles off the
        index): a permanent give-up the caller must surface loudly. redis-py's
        async ``eval`` stub types a non-awaitable return; it is awaitable at runtime.
        """
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
        raw_asked_by = fields.get("asked_by")
        raw_delivery = fields.get("delivery")
        raw_landing = fields.get("caller_ask_landing")
        raw_binding = fields.get("deferred_binding")
        raw_run_input = fields.get("run_input")
        raw_params = fields.get("params")
        raw_stages = fields.get("deferred_output_stages")
        return ContinuationDue(
            interaction_id=interaction_id,
            tool=fields["tool"],
            identity=fields["identity"],
            fingerprint=fields["fingerprint"],
            answer=json.loads(fields["answer"]),
            attempts=int(fields["attempts"]),
            state_context=StateContext.model_validate_json(raw_context) if raw_context is not None else None,
            asked_by=json.loads(raw_asked_by) if raw_asked_by is not None else [],
            delivery=DeliveryAddress.model_validate_json(raw_delivery) if raw_delivery is not None else None,
            run_delivery_id=fields.get("run_delivery_id"),
            caller_ask_landing=CallerAskLanding.model_validate_json(raw_landing) if raw_landing is not None else None,
            deferred_binding=StateBinding.model_validate_json(raw_binding) if raw_binding is not None else None,
            run_input=json.loads(raw_run_input) if raw_run_input is not None else None,
            door_id=fields.get("door_id"),
            chain_keys=decode_chain_keys(fields),
            reply_to_medium_message_id=fields.get("reply_to_medium_message_id"),
            params=json.loads(raw_params) if raw_params is not None else None,
            deferred_output_stages=(
                PresetParkStages.model_validate_json(raw_stages) if raw_stages is not None else None
            ),
        )

    async def due_kills(self, r: Redis, now: datetime) -> list[str]:
        """The interaction ids of kill-due records whose next-attempt time is at or before ``now``.

        The kill-due reaper leg's work list, mirroring :meth:`due_continuations`. A member survives
        until the kill's teardown, channel withdrawal and terminal delivery commit
        (``clear_kill_due``), or the reaper gives it up past its deadline.
        """
        cutoff = int(now.timestamp() * 1000)
        raw = await r.zrangebyscore(self.kill_due_index_key, 0, cutoff)
        return [serde.as_str(member) for member in raw]

    async def claim_kill_retry(
        self, r: Redis, interaction_id: str, now: datetime, backoff_base_ms: int, backoff_cap_ms: int
    ) -> KillDue | KillRetryDrop | None:
        """Atomically claim a due kill-due record for redelivery, advancing its backoff.

        Reuses the continuation retry-claim script (index + record key generic): advances the
        record's next-attempt score by an exponential backoff and returns it to re-fire, ``None``
        when not (or no longer) due, and :data:`KILL_DROPPED` when the record hash TTL-expired and
        the orphan index member is reconciled off. The reaper's ordinary give-up is governed by the
        record's own ``deadline_ms`` (read here while the record still stands), not by this drop.
        The record's ``terminal`` is REQUIRED: a record without it raises ``KeyError`` rather than
        being read as any terminal. redis-py's async ``eval`` stub types a non-awaitable return; it
        is awaitable at runtime.
        """
        raw = await cast(
            "Awaitable[list[Any] | str | bytes | None]",
            r.eval(
                scripts._CONTINUATION_RETRY_CLAIM_LUA,
                2,
                self.kill_due_index_key,
                self.kill_due_key(interaction_id),
                interaction_id,
                str(int(now.timestamp() * 1000)),
                str(backoff_base_ms),
                str(backoff_cap_ms),
            ),
        )
        if not raw:
            return None
        if raw in (b"dropped", "dropped"):
            return KILL_DROPPED
        it = iter(cast("list[Any]", raw))
        fields = {serde.as_str(k): serde.as_str(v) for k, v in zip(it, it, strict=True)}
        raw_delivery = fields.get("delivery")
        raw_subjects = fields.get("subjects")
        return KillDue(
            interaction_id=interaction_id,
            reason=fields.get("reason", ""),
            attempts=int(fields["attempts"]),
            deadline_ms=int(fields["deadline_ms"]),
            terminal=fields["terminal"],
            delivery=DeliveryAddress.model_validate_json(raw_delivery) if raw_delivery is not None else None,
            run_delivery_id=fields.get("run_delivery_id"),
            subjects=json.loads(raw_subjects) if raw_subjects is not None else None,
            chain_keys=decode_chain_keys(fields),
            channel=fields.get("channel"),
            recipient=fields.get("recipient"),
            continuation_tool=fields.get("continuation_tool"),
        )

    async def due_untaken_outcomes(self, r: Redis, now: datetime, horizon_seconds: int) -> list[str]:
        """The ``completion_id``s of waiting outcomes older than the retention horizon — the sweep's work list.

        Reads the retention index by score for members created at or before ``now - horizon_seconds``.
        A member leaves the index when its outcome is taken, killed/erased or swept, so a listed id is
        an untaken outcome the sweep drops (atomically, via :meth:`claim_outcome`, so a racing take or
        a concurrent sweep pass resolves to exactly one winner).
        """
        cutoff = int(now.timestamp() * 1000) - horizon_seconds * 1000
        raw = await r.zrangebyscore(self.outcome_retention_index_key, 0, cutoff)
        return [serde.as_str(member) for member in raw]
