"""The Celery schedule store: each schedule a RedBeat entry firing ``tool_execution``.

A schedule is RedBeat's entry hash ``redbeat:{name}`` (its ``definition`` field holds the
name, args, kwargs, cadence and enabled flag) plus its member in the schedule zset
``redbeat::schedule``, keyed by the entry key and scored with the next run's timestamp. An
enabled entry is a zset member; disabling an entry removes the member and keeps the hash.
The reads go through the host's pooled Redis client; the write saves through RedBeat itself.
A name is taken bare or with RedBeat's key prefix.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AbstractAsyncContextManager
from typing import Any

from pydantic_core import to_jsonable_python
from redbeat import RedBeatSchedulerEntry
from tai42_contract.app import tai42_app
from tai42_contract.backend import CrontabCadence, IntervalCadence, ScheduleRecord, StoredSchedule
from tai42_kit.backend import fired_kwargs
from tai42_kit.clients.impl.redis import RedisClient

from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.redbeat_mapping import from_redbeat, to_celery_schedule, to_redbeat
from tai42_backend_celery.core.settings import CelerySettings, celery_settings


def _redbeat_redis() -> AbstractAsyncContextManager[Any]:
    """The pooled async Redis client for the RedBeat store, typed ``Any``.

    It connects with the full ``CELERY_REDBEAT_*`` connection settings, read as bytes
    (:func:`_text` decodes). redis-py annotates each command as a sync/async union that
    cannot be awaited as-is.
    """
    return tai42_app.clients.client_ctx(
        RedisClient, **{**celery_settings().redbeat.client_kwargs(), "decode_responses": False}
    )


def _text(value: Any) -> Any:
    """Decode Redis bytes to text (the pooled client may return either)."""
    return value.decode() if isinstance(value, bytes | bytearray) else value


async def _read_entry(r: Any, settings: CelerySettings, key: str) -> tuple[dict[str, Any], float | None] | None:
    """The entry's parsed ``definition`` and its zset score, read at one server instant.

    One MULTI/EXEC pipeline reads both, so a concurrent delete and re-create cannot show a
    state the store never passed through. ``None`` when the entry and its member are both
    gone; a member with no ``definition`` beside it raises ``KeyError``.
    """
    async with r.pipeline(transaction=True) as pipe:
        pipe.hget(key, "definition")
        pipe.zscore(settings.redbeat_schedule_key, key)
        definition, score = await pipe.execute()
    if not definition:
        if score is None:
            return None
        raise KeyError(f"schedule zset lists {key!r} but hash {key!r} has no 'definition'")
    return json.loads(_text(definition)), None if score is None else float(score)


def _stored(settings: CelerySettings, key: str, definition: dict[str, Any], score: float | None) -> StoredSchedule:
    return StoredSchedule(
        name=key.removeprefix(settings.redbeat_key_prefix),
        args=list(definition.get("args") or []),
        kwargs=dict(definition.get("kwargs") or {}),
        cadence=from_redbeat(definition["schedule"]),
        enabled=bool(definition.get("enabled", True)),
        next_run_at_ts=score,
    )


class CeleryScheduleStore:
    """The RedBeat store of Celery's schedules."""

    @property
    def relative_intervals(self) -> bool:
        """``True``: RedBeat honours an interval's ``relative`` flag (``redbeat_mapping.to_celery_schedule``)."""
        return True

    @property
    def holds_disabled(self) -> bool:
        """``True``: a disabled entry keeps its hash, out of the schedule zset."""
        return True

    async def write(self, record: ScheduleRecord) -> None:
        """Save ``record`` as the RedBeat entry of its name; RedBeat computes its next run from the cadence."""
        entry = RedBeatSchedulerEntry(
            name=record.name,
            task="celery.tool_execution",
            schedule=to_celery_schedule(record.cadence),
            args=to_jsonable_python(record.args),
            kwargs=to_jsonable_python(fired_kwargs(record, celery_settings().tool_name_arg)),
            enabled=record.enabled,
            app=celery_app,
        )
        await asyncio.to_thread(entry.save)

    async def exists(self, name: str) -> bool:
        """Whether the entry hash of ``name`` exists."""
        async with _redbeat_redis() as r:
            return bool(await r.exists(celery_settings().redbeat_task_key(name)))

    async def read(self, name: str) -> StoredSchedule | None:
        """The entry of ``name``, its next run the zset score (``None`` when disabled)."""
        settings = celery_settings()
        key = settings.redbeat_task_key(name)
        async with _redbeat_redis() as r:
            entry = await _read_entry(r, settings, key)
        return None if entry is None else _stored(settings, key, *entry)

    async def read_all(self) -> list[StoredSchedule]:
        """Every entry: the zset members, then the entry hashes a key scan finds outside the zset.

        The scan finds the disabled entries, which have no member. RedBeat's own keys (the
        ``redbeat::`` namespace: the zset, its lock, its statics) are left out.
        """
        settings = celery_settings()
        internal_prefix = f"{settings.redbeat_key_prefix}:"
        async with _redbeat_redis() as r:
            keys = [_text(member) for member in await r.zrange(settings.redbeat_schedule_key, 0, -1)]
            seen = set(keys)
            async for scanned in r.scan_iter(match=f"{settings.redbeat_key_prefix}*"):
                key = _text(scanned)
                if key.startswith(internal_prefix) or key in seen:
                    continue
                seen.add(key)
                keys.append(key)
            stored: list[StoredSchedule] = []
            for key in keys:
                entry = await _read_entry(r, settings, key)
                if entry is not None:
                    stored.append(_stored(settings, key, *entry))
        return stored

    async def delete(self, name: str) -> bool:
        """Remove the zset member, then the entry hash (RedBeat's own order, no dangling member)."""
        settings = celery_settings()
        key = settings.redbeat_task_key(name)
        async with _redbeat_redis() as r:
            if not await r.exists(key):
                return False
            await r.zrem(settings.redbeat_schedule_key, key)
            await r.delete(key)
        return True

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        """Set the definition's flag; enabling scores the entry 0 (due at Beat's next tick), disabling drops it."""
        settings = celery_settings()
        key = settings.redbeat_task_key(name)
        async with _redbeat_redis() as r:
            definition = await r.hget(key, "definition")
            if not definition:
                return False
            parsed = json.loads(_text(definition))
            parsed["enabled"] = enabled
            await r.hset(key, "definition", json.dumps(parsed))
            if enabled:
                await r.zadd(settings.redbeat_schedule_key, {key: 0})
            else:
                await r.zrem(settings.redbeat_schedule_key, key)
        return True

    async def run_now(self, name: str) -> bool:
        """Score the entry 0, so Beat fires it at its next tick."""
        settings = celery_settings()
        key = settings.redbeat_task_key(name)
        async with _redbeat_redis() as r:
            if not await r.exists(key):
                return False
            await r.zadd(settings.redbeat_schedule_key, {key: 0})
        return True

    async def update(
        self,
        name: str,
        *,
        cadence: IntervalCadence | CrontabCadence | None,
        next_run_at_ts: float | None,
    ) -> bool:
        """Rewrite the definition's cadence and rescore the entry, as given.

        A new cadence without a next run keeps the entry's current score: Beat fires it then,
        and RedBeat computes each run after from the new cadence.
        """
        settings = celery_settings()
        key = settings.redbeat_task_key(name)
        async with _redbeat_redis() as r:
            definition = await r.hget(key, "definition")
            if not definition:
                return False
            if cadence is not None:
                parsed = json.loads(_text(definition))
                parsed["schedule"] = to_redbeat(cadence)
                await r.hset(key, "definition", json.dumps(parsed))
            if next_run_at_ts is not None:
                await r.zadd(settings.redbeat_schedule_key, {key: float(next_run_at_ts)})
        return True
