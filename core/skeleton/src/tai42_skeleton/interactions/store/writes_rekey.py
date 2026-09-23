"""Subject re-keying across every denormalized copy — the person merge.

Move every entry addressed to one subject key onto another, rewriting each denormalized subject copy
(state hash, continuation-due, kill-due, outcome hash) so every later leg addresses the new key.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from typing import cast

from redis.asyncio import Redis
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
