"""Redis-backed hooks manager: topic-indexed hook storage, webhook replay claims and the hook-run store."""

import json
import logging
from typing import Any, Literal

from tai42_contract.hooks.models import HookParams, TopicVerifierBinding
from tai42_contract.interactions import VisitAct
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.hooks.managers.base_hooks_manager import BaseHooksManager
from tai42_skeleton.hooks.managers.redis_hook_runs import (
    ACCEPT_LUA,
    CLAIM_DUE_LUA,
    CLEAR_INTENT_LUA,
    DISCARD_LUA,
    FAIL_LUA,
    FINISH_LUA,
    MARK_LUA,
    RELEASE_LUA,
    RENEW_LUA,
    RESCHEDULE_LUA,
    RETRY_LUA,
)
from tai42_skeleton.hooks.runs.models import (
    ClaimIntent,
    HookRun,
    HookRunActed,
    HookRunCounts,
    HookRunsPage,
    ReplayClaim,
    RetryResult,
    StartSpec,
    page_offset,
)
from tai42_skeleton.hooks.settings import HooksSettings
from tai42_skeleton.utils.redis_typing import awaited, eval_script

logger = logging.getLogger(__name__)

# The name->topic map read and the per-topic hash writes must be ONE atomic unit,
# or two concurrent moves of the same name (or a register racing an unregister)
# can leave a hook double-listed under a stale topic or orphaned. Each op is a
# single server-side Lua step. KEYS[1] is the name->topic map; the per-topic hook
# hash keys are built inside Lua from the ``prefix`` ARGV to mirror
# ``HooksSettings.get_hook_key`` (``f"{prefix}:topic:{topic}"``) — the prior
# topic's key is not known until the map is read.
#
# register: KEYS[1]=map, ARGV = prefix, topic, name, hook_json
_REGISTER_LUA = """
-- hooks:register:atomic
local map_key = KEYS[1]
local prefix, topic, name, hook_json = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local prev_topic = redis.call('HGET', map_key, name)
if prev_topic and prev_topic ~= topic then
  redis.call('HDEL', prefix .. ':topic:' .. prev_topic, name)
end
redis.call('HSET', prefix .. ':topic:' .. topic, name, hook_json)
redis.call('HSET', map_key, name, topic)
return 1
"""

# unregister: KEYS[1]=map, ARGV = prefix, name
_UNREGISTER_LUA = """
-- hooks:unregister:atomic
local map_key = KEYS[1]
local prefix, name = ARGV[1], ARGV[2]
local topic = redis.call('HGET', map_key, name)
if not topic then
  return 0
end
local removed = redis.call('HDEL', prefix .. ':topic:' .. topic, name)
local removed_map = redis.call('HDEL', map_key, name)
if removed > 0 or removed_map > 0 then
  return 1
end
return 0
"""


class RedisHooksManager(BaseHooksManager):
    """Hooks manager storing hook registrations, topic verifiers and hook-run jobs in redis."""

    def __init__(self, settings: HooksSettings):
        """Bind the manager to ``settings`` (redis connection and key layout)."""
        super().__init__(settings)

    async def _store_hook(self, params: HookParams) -> bool:
        """Store or move ``params`` under its topic atomically; returns ``True``."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            await eval_script(
                r,
                _REGISTER_LUA,
                1,
                self.settings.name_trigger_map_key,
                self.settings.prefix,
                params.topic,
                params.name,
                params.model_dump_json(),
            )
        return True

    async def unregister(self, name: str) -> bool:
        """Remove the named hook atomically; returns whether one was present."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            removed = await eval_script(
                r,
                _UNREGISTER_LUA,
                1,
                self.settings.name_trigger_map_key,
                self.settings.prefix,
                name,
            )
        return bool(removed)

    async def list_hooks_by_topic(self, topic: str) -> dict[str, HookParams]:
        """Return the hooks registered under ``topic``, keyed by name."""
        key = self.settings.get_hook_key(topic)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            data = await awaited(r.hgetall(key))
            return {name: HookParams.model_validate_json(hook_json) for name, hook_json in data.items()} if data else {}

    async def list_hooks(self) -> dict[str, HookParams]:
        """Return every registered hook across all topics, keyed by name."""
        hooks: dict[str, HookParams] = {}
        async with client_ctx(RedisClient, self.settings.redis) as r:
            name_topic_map = await awaited(r.hgetall(self.settings.name_trigger_map_key))
            if not name_topic_map:
                return {}

            pipe = r.pipeline()
            names = []

            for name, topic in name_topic_map.items():
                key = self.settings.get_hook_key(topic)
                pipe.hget(key, name)
                names.append(name)

            results = await pipe.execute()

            for name, hook_json in zip(names, results, strict=True):
                if hook_json:
                    hooks[name] = HookParams.model_validate_json(hook_json)
        return hooks

    async def set_topic_verifier(self, topic: str, binding: dict[str, Any]) -> None:
        """Store the verifier ``binding`` for ``topic``, validating its shape on write."""
        # Validate the shape on write so a malformed binding can never be stored;
        # persist the canonical JSON.
        model = TopicVerifierBinding.model_validate(binding)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            await awaited(r.hset(self.settings.topic_verifiers_key, topic, model.model_dump_json()))

    async def get_topic_verifier(self, topic: str) -> TopicVerifierBinding | None:
        """Return ``topic``'s verifier binding, or ``None`` when none is set."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            raw = await awaited(r.hget(self.settings.topic_verifiers_key, topic))
        if not raw:
            return None
        # Validate on read: a wrong-shape stored value raises here (loud).
        return TopicVerifierBinding.model_validate_json(raw)

    async def delete_topic_verifier(self, topic: str) -> bool:
        """Delete ``topic``'s verifier binding; returns whether one was removed."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            removed = await awaited(r.hdel(self.settings.topic_verifiers_key, topic))
        return removed > 0

    async def all_topic_verifiers(self) -> dict[str, TopicVerifierBinding]:
        """Return every topic's verifier binding, keyed by topic."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            data = await awaited(r.hgetall(self.settings.topic_verifiers_key))
        # Validate every entry on read (loud on a wrong shape), matching the
        # single-topic read.
        return {topic: TopicVerifierBinding.model_validate_json(binding) for topic, binding in data.items()}

    # -- Hook runs -------------------------------------------------------------

    async def _accept_runs(
        self,
        topic: str,
        jobs: list[HookRun],
        replay_claim: ReplayClaim | None,
        claimed_by: str | None,
        lease_until: float | None,
    ) -> bool:
        """Write ``jobs`` and take ``replay_claim`` in one script; False (nothing written) when already seen."""
        payload = json.dumps(
            [
                {
                    "id": job.id,
                    "score": repr(
                        lease_until if claimed_by is not None and lease_until is not None else job.next_attempt_at
                    ),
                    "fields": job.to_fields(),
                }
                for job in jobs
            ]
        )
        keys = [self.settings.hook_runs_due_key]
        ttl = "0"
        if replay_claim is not None:
            keys.append(self.settings.webhook_seen_key(topic, replay_claim.key))
            ttl = str(replay_claim.ttl_seconds)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            accepted = await eval_script(r, ACCEPT_LUA, len(keys), *keys, self.settings.prefix, payload, ttl)
        return bool(accepted)

    async def _claim_due_runs(
        self, now: float, limit: int, claimed_by: str, lease_until: float, exclude: frozenset[str]
    ) -> list[HookRun]:
        """Claim up to ``limit`` due jobs in one script."""
        if limit <= 0:
            return []
        async with client_ctx(RedisClient, self.settings.redis) as r:
            raw = await eval_script(
                r,
                CLAIM_DUE_LUA,
                1,
                self.settings.hook_runs_due_key,
                self.settings.prefix,
                repr(now),
                limit,
                claimed_by,
                repr(lease_until),
                json.dumps(sorted(exclude)),
            )
        return [HookRun.from_fields(_pairs(raw[i + 1]), reclaimed=raw[i] == "running") for i in range(0, len(raw), 2)]

    async def _fenced(self, script: str, op: str, run_id: str, keys: list[str], *args: object) -> bool:
        async with client_ctx(RedisClient, self.settings.redis) as r:
            done = await eval_script(r, script, len(keys), *keys, *args)
        if not done:
            _log_lease_lost(run_id, op)
        return bool(done)

    async def _renew_lease(self, run_id: str, claimed_by: str, lease_until: float) -> bool:
        """Extend the lease of a job this claimer holds."""
        return await self._fenced(
            RENEW_LUA,
            "renew its lease",
            run_id,
            [self.settings.hook_run_key(run_id), self.settings.hook_runs_due_key],
            run_id,
            claimed_by,
            repr(lease_until),
        )

    async def _mark_acted(self, run_id: str, claimed_by: str, act: VisitAct, start_spec: StartSpec | None) -> bool:
        """Record a visit notice on the job."""
        intent = (
            ClaimIntent(action=act.action, ids=act.interaction_ids).to_json()
            if act.phase == "before" and act.action in ("resume", "take")
            else ""
        )
        return await self._fenced(
            MARK_LUA,
            f"record its {act.action} {act.phase} notice",
            run_id,
            [self.settings.hook_run_key(run_id)],
            claimed_by,
            act.phase,
            act.action,
            json.dumps(list(act.interaction_ids)),
            intent,
            "" if start_spec is None else start_spec.to_json(),
        )

    async def _clear_intent(self, run_id: str, claimed_by: str) -> bool:
        """Clear the job's claim intent."""
        return await self._fenced(
            CLEAR_INTENT_LUA, "clear its claim intent", run_id, [self.settings.hook_run_key(run_id)], claimed_by
        )

    async def _finish_run(self, run_id: str, claimed_by: str) -> bool:
        """Delete a job that succeeded."""
        return await self._fenced(
            FINISH_LUA,
            "record its success",
            run_id,
            [self.settings.hook_run_key(run_id), self.settings.hook_runs_due_key],
            run_id,
            claimed_by,
        )

    async def _reschedule_run(
        self, run_id: str, claimed_by: str, next_attempt_at: float, error: str, kind: str
    ) -> bool:
        """Set the job pending, due at ``next_attempt_at``."""
        return await self._fenced(
            RESCHEDULE_LUA,
            "schedule its retry",
            run_id,
            [self.settings.hook_run_key(run_id), self.settings.hook_runs_due_key],
            run_id,
            claimed_by,
            repr(next_attempt_at),
            error,
            kind,
        )

    async def _fail_run(
        self,
        run_id: str,
        claimed_by: str,
        error: str,
        kind: str,
        failed_at: float,
        acted: HookRunActed,
        acted_ids: tuple[str, ...],
        claim_intent: ClaimIntent | None,
    ) -> HookRun | None:
        """Hold the job as failed; return it, or ``None`` when the lease was lost."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            raw = await eval_script(
                r,
                FAIL_LUA,
                3,
                self.settings.hook_run_key(run_id),
                self.settings.hook_runs_due_key,
                self.settings.hook_runs_failed_key,
                run_id,
                claimed_by,
                error,
                kind,
                repr(failed_at),
                acted,
                json.dumps(list(acted_ids)) if acted_ids else "",
                "" if claim_intent is None else claim_intent.to_json(),
            )
        if not raw:
            _log_lease_lost(run_id, "record its failure")
            return None
        return HookRun.from_fields(_pairs(raw))

    async def _release_run(self, run_id: str, claimed_by: str, now: float) -> bool:
        """Set the job pending and due now."""
        return await self._fenced(
            RELEASE_LUA,
            "release it",
            run_id,
            [self.settings.hook_run_key(run_id), self.settings.hook_runs_due_key],
            run_id,
            claimed_by,
            repr(now),
        )

    async def list_hook_runs(
        self, status: Literal["outstanding", "failed"] | None, limit: int, cursor: str | None
    ) -> HookRunsPage:
        """One page of hook runs: outstanding ones soonest due first, then failed ones most recent first."""
        offset = page_offset(cursor)
        async with client_ctx(RedisClient, self.settings.redis) as r:
            ids: list[str] = []
            if status in (None, "outstanding"):
                ids = list(await awaited(r.zrange(self.settings.hook_runs_due_key, offset, offset + limit - 1)))
            if status in (None, "failed") and len(ids) < limit:
                failed_offset = offset
                if status is None:
                    due_count = int(await awaited(r.zcard(self.settings.hook_runs_due_key)))
                    failed_offset = max(0, offset - due_count)
                ids += list(
                    await awaited(
                        r.zrevrange(
                            self.settings.hook_runs_failed_key,
                            failed_offset,
                            failed_offset + limit - len(ids) - 1,
                        )
                    )
                )
            pipe = r.pipeline()
            for run_id in ids:
                pipe.hgetall(self.settings.hook_run_key(run_id))
            rows = await pipe.execute() if ids else []
        items = tuple(HookRun.from_fields(row) for row in rows if row)
        return HookRunsPage(items=items, next_cursor=str(offset + limit) if len(ids) == limit else None)

    async def count_hook_runs(self) -> HookRunCounts:
        """How many hook runs are outstanding and how many are failed."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            pipe = r.pipeline()
            pipe.zcard(self.settings.hook_runs_due_key)
            pipe.zcard(self.settings.hook_runs_failed_key)
            outstanding, failed = await pipe.execute()
        return HookRunCounts(outstanding=int(outstanding), failed=int(failed))

    async def retry_hook_run(
        self, run_id: str, now: float, *, claimed_by: str | None = None, lease_until: float | None = None
    ) -> RetryResult:
        """Re-queue a failed job as a fresh run, in one script."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            raw = await eval_script(
                r,
                RETRY_LUA,
                3,
                self.settings.hook_run_key(run_id),
                self.settings.hook_runs_due_key,
                self.settings.hook_runs_failed_key,
                run_id,
                repr(now),
                claimed_by or "",
                "" if lease_until is None else repr(lease_until),
            )
        outcome = raw[0]
        run = HookRun.from_fields(_pairs(raw[1])) if len(raw) > 1 else None
        return RetryResult(outcome=outcome, run=run)

    async def discard_hook_run(self, run_id: str) -> Literal["ok", "missing", "not_failed"]:
        """Delete a failed job, in one script."""
        async with client_ctx(RedisClient, self.settings.redis) as r:
            return await eval_script(
                r,
                DISCARD_LUA,
                3,
                self.settings.hook_run_key(run_id),
                self.settings.hook_runs_due_key,
                self.settings.hook_runs_failed_key,
                run_id,
            )


def _pairs(flat: list[str]) -> dict[str, str]:
    """A hash from the flat field/value list a script's HGETALL returns."""
    return dict(zip(flat[::2], flat[1::2], strict=True))


def _log_lease_lost(run_id: str, op: str) -> None:
    logger.warning("hook run %s: this process no longer holds its lease, so it did not %s", run_id, op)
