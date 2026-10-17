"""Subject re-keying across every denormalized copy — the person merge.

Move every entry addressed to one subject key onto another, rewriting each denormalized subject copy
(state hash, continuation-due, kill-due, outcome hash) so every later leg addresses the new key.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import WatchError
from tai42_contract.conversation_target import ConversationTargetKind
from tai42_contract.interactions import InteractionRequest
from tai42_contract.states import StateContext

from . import serde
from .keys import _StoreKeys


class _StoreRekeyWrites(_StoreKeys):
    """Subject re-keying across every denormalized copy (the person merge)."""

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
        new_scopes_key = self.subject_scopes_key(kind, new_key)
        scope_tokens = [
            serde.as_str(token) for token in await cast("Awaitable[set[str | bytes]]", r.smembers(scopes_key))
        ]
        moved: list[str] = []
        for token in scope_tokens:
            scope_kind, _, scope_name = token.partition(":")
            scope_target_kind = cast("ConversationTargetKind", scope_kind)
            old_parks = self.subject_parks_key(scope_target_kind, scope_name, kind, old_key)
            new_parks = self.subject_parks_key(scope_target_kind, scope_name, kind, new_key)

            async def _rekey_members(members: list[str]) -> None:
                for member in members:
                    await self._rekey_member(r, member, kind, old_key, new_key)

            moved.extend(await self._move_set_members(r, old_parks, new_parks, before_move=_rekey_members))
            await self._move_set_members(r, scopes_key, new_scopes_key, only=token)
        return moved

    @staticmethod
    async def _move_set_members(
        r: Redis,
        old_key: str,
        new_key: str,
        *,
        only: str | None = None,
        before_move: Callable[[list[str]], Awaitable[None]] | None = None,
    ) -> list[str]:
        """Move the members of the set at ``old_key`` (or just ``only``) onto ``new_key``; return those moved.

        The old index sets carry the set-or-extend-to-greater TTL every park/outcome write stamps,
        so they outlive the entries they index. The move carries that horizon onto the new set to the
        millisecond (``PEXPIRE NX`` sets it on a set the merge created fresh, ``GT`` only raises an
        existing one), so a merge never leaves a new index set with no expiry (a permanent leak) or one
        shorter than the moved entries. It is one guarded read-modify-write: the old set is WATCHed,
        its TTL and members are read together, ``before_move`` runs on the members, and the move is
        written under MULTI/EXEC, so a write, a delete or a TTL lapse of the old set between the read
        and the write aborts the EXEC and the set is read again. Nothing to move (the old set is gone,
        or ``only`` is not a member) is a no-op.
        """
        async with r.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(old_key)
                    ttl_ms = await cast("Awaitable[int]", pipe.pttl(old_key))
                    members = [
                        serde.as_str(m) for m in await cast("Awaitable[set[str | bytes]]", pipe.smembers(old_key))
                    ]
                    if only is not None:
                        members = [m for m in members if m == only]
                    if not members:
                        await pipe.reset()
                        return []
                    if before_move is not None:
                        await before_move(members)
                    pipe.multi()
                    pipe.sadd(new_key, *members)
                    pipe.srem(old_key, *members)
                    if ttl_ms > 0:
                        pipe.pexpire(new_key, ttl_ms, nx=True)
                        pipe.pexpire(new_key, ttl_ms, gt=True)
                    await pipe.execute()
                except WatchError:
                    continue
                return members

    async def _rekey_member(self, r: Redis, member: str, kind: str, old_key: str, new_key: str) -> None:
        """Rewrite every stored subject copy of one moved entry so ``by_kind[kind]`` points at ``new_key``.

        A moved member's subject descriptor is denormalized in more than one durable place, each read
        by a different leg after the merge — so all are re-keyed in the same walk:

        * the state hash (a park / running entry: ``subjects``, ``request``, ``continuation_state_context``);
        * the continuation-due record's ``state_context`` copy, which a detached redelivery reads to
          subject-track the resumed run's outcome;
        * the kill-due record's ``subjects`` copy, which a crash-redelivery reads to subject-track the
          killed run's terminal;
        * the outcome hash (a waiting outcome: ``subjects``).

        Each is a guarded read-modify-write (:meth:`_rewrite_hash`), a no-op when the record is absent
        or already names the new key, so a re-park, a continuation redelivery, a kill redelivery and an
        index-leave after the merge all address the survivor's key.
        """
        await self._rewrite_hash(
            r, self.state_key(member), lambda fields: self._rekey_state_fields(fields, kind, old_key, new_key)
        )
        await self._rewrite_hash(
            r,
            self.continuation_due_key(member),
            lambda fields: self._rekey_continuation_due_fields(fields, kind, old_key, new_key),
        )
        await self._rewrite_hash(
            r, self.kill_due_key(member), lambda fields: self._rekey_subjects_field(fields, kind, old_key, new_key)
        )
        await self._rewrite_hash(
            r, self.outcome_key(member), lambda fields: self._rekey_subjects_field(fields, kind, old_key, new_key)
        )

    @staticmethod
    async def _rewrite_hash(r: Redis, key: str, rewrite: Callable[[dict[str, str]], dict[str, str]]) -> None:
        """Write the field updates ``rewrite`` derives from the hash at ``key``, as one guarded read-modify-write.

        The hash is WATCHed, read, and the updates are written under MULTI/EXEC, so a write, a delete
        or a TTL lapse of the hash between the read and the write aborts the EXEC and the hash is read
        again. An absent hash, or one ``rewrite`` leaves unchanged (an empty mapping), is a no-op: the
        merge never writes a record that is gone, which would leave a partial hash with no TTL.
        """
        async with r.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(key)
                    raw = await cast("Awaitable[dict[str | bytes, str | bytes]]", pipe.hgetall(key))
                    updates = rewrite({serde.as_str(k): serde.as_str(v) for k, v in raw.items()}) if raw else {}
                    if not updates:
                        await pipe.reset()
                        return
                    pipe.multi()
                    pipe.hset(key, mapping=updates)
                    await pipe.execute()
                except WatchError:
                    continue
                return

    @classmethod
    def _rekey_state_fields(cls, fields: dict[str, str], kind: str, old_key: str, new_key: str) -> dict[str, str]:
        """The state hash's re-keyed ``subjects`` descriptor, ``request`` and ``continuation_state_context``."""
        updates = cls._rekey_subjects_field(fields, kind, old_key, new_key)
        request = InteractionRequest.model_validate_json(fields["request"])
        new_ctx = cls._rekey_context(request.continuation_state_context, kind, old_key, new_key)
        if new_ctx is not None:
            updates["request"] = request.model_copy(update={"continuation_state_context": new_ctx}).model_dump_json()
            updates["continuation_state_context"] = new_ctx.model_dump_json()
        return updates

    @classmethod
    def _rekey_continuation_due_fields(
        cls, fields: dict[str, str], kind: str, old_key: str, new_key: str
    ) -> dict[str, str]:
        """The continuation-due record's re-keyed ``state_context`` copy the redelivery leg reads."""
        context_field = fields.get("state_context")
        if context_field is None:
            return {}
        new_ctx = cls._rekey_context(StateContext.model_validate_json(context_field), kind, old_key, new_key)
        return {} if new_ctx is None else {"state_context": new_ctx.model_dump_json()}

    @staticmethod
    def _rekey_subjects_field(fields: dict[str, str], kind: str, old_key: str, new_key: str) -> dict[str, str]:
        """A re-keyed ``subjects`` descriptor (state hash, kill-due record, outcome hash), empty when unaffected."""
        subjects_field = fields.get("subjects")
        if subjects_field is None:
            return {}
        descriptor = json.loads(subjects_field)
        if descriptor["by_kind"].get(kind) != old_key:
            return {}
        descriptor["by_kind"][kind] = new_key
        return {"subjects": json.dumps(descriptor)}

    @staticmethod
    def _rekey_context(context: StateContext | None, kind: str, old_key: str, new_key: str) -> StateContext | None:
        """Return ``context`` with ``candidates.by_kind[kind]`` re-keyed to ``new_key``, or ``None`` when unaffected."""
        if context is None or context.candidates.by_kind.get(kind) != old_key:
            return None
        new_by_kind = dict(context.candidates.by_kind)
        new_by_kind[kind] = new_key
        new_candidates = context.candidates.model_copy(update={"by_kind": new_by_kind})
        return context.model_copy(update={"candidates": new_candidates})
