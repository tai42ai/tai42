"""Waiting-outcome writes: park a resumed run's terminal, take it, reconcile and delete.

Park the finished/failed outcome a subject owner later takes, claim it atomically, reconcile an
orphaned index member, and delete an outcome unconditionally.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import Any, Literal, cast

from redis.asyncio import Redis
from redis.exceptions import WatchError
from tai42_contract.states import SubjectCandidates

from . import records, serde, ttl
from .keys import _StoreKeys
from .records import WaitingOutcome


class _StoreOutcomeWrites(_StoreKeys):
    """Waiting-outcome writes: park, claim, reconcile, delete."""

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
