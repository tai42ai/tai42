"""The uniform ``backend_*`` tool surface over Celery.

Task/worker tools use Celery's control ``inspect`` API and ``AsyncResult``;
schedule tools operate directly on RedBeat's Redis structures (the entry hash
``redbeat:{name}`` and the schedule zset ``redbeat::schedule`` whose members are
the entry keys), reached through the host's pooled Redis client. Every schedule
tool addresses the zset by entry key and accepts ``name`` bare or prefixed. The
host's schedule marker tools are ``backend_list_schedules`` /
``backend_delete_schedule`` / ``backend_export_schedules`` /
``backend_import_schedules``.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

from celery.exceptions import TimeoutError as CeleryTimeoutError
from celery.result import AsyncResult
from redbeat import RedBeatSchedulerEntry
from tai42_contract.app import tai42_app
from tai42_contract.backend import (
    ScheduleImportError,
    ScheduleImportMode,
    ScheduleImportReport,
    ScheduleListRow,
    ScheduleRecord,
)
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.utils.runtime.schedule_util import normalize_schedule

from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.redbeat_mapping import from_redbeat, to_celery_schedule, to_redbeat
from tai42_backend_celery.core.settings import celery_settings


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


def _fired_tool(name: str, kwargs: dict[str, Any]) -> str:
    """Pop and return the fired tool's name from a stored entry's ``kwargs``.

    A stored entry without the tool-name key was not created through this backend's schedule
    door; it raises rather than being reported without a tool.
    """
    tool_name_arg = celery_settings().tool_name_arg
    if tool_name_arg not in kwargs:
        raise ValueError(
            f"schedule {name!r} carries no {tool_name_arg!r}; it was not created through this backend's schedule door"
        )
    return kwargs.pop(tool_name_arg)


async def _read_definition_and_membership(r: Any, schedule_key: str, entry_key: str) -> tuple[Any, Any]:
    """Read an entry's ``definition`` field and its schedule-zset score at one server instant.

    Uses a single MULTI/EXEC pipeline. The atomic read drives the listing tools'
    dangling-member check: read separately, a concurrent delete+re-create could show a
    false "no definition next to member present" state the store never actually passed
    through.
    """
    async with r.pipeline(transaction=True) as pipe:
        pipe.hget(entry_key, "definition")
        pipe.zscore(schedule_key, entry_key)
        definition, member_score = await pipe.execute()
    return definition, member_score


@tai42_app.tools.tool(tags={"backend"})
def backend_ping_worker(worker_name: str | None = None) -> dict:
    """Ping all workers or a specific worker to check if they're alive."""
    inspect = celery_app.control.inspect([worker_name] if worker_name else None)
    return inspect.ping() or {}


@tai42_app.tools.tool(tags={"backend"})
def backend_list_active_workers() -> list:
    """Get a list of all currently connected and responsive worker names."""
    inspect = celery_app.control.inspect()
    response = inspect.ping() or {}
    return list(response.keys())


@tai42_app.tools.tool(tags={"backend"})
def backend_task_status(task_id: str) -> str:
    """Return the current status of a given task ID."""
    result = AsyncResult(task_id, app=celery_app)
    return result.status


@tai42_app.tools.tool(tags={"backend"})
def backend_task_result(task_id: str, timeout: float | None = None) -> Any:
    """Return the result of a completed task by ID.

    When ``timeout`` is given, wait up to that many seconds for the task to
    finish before reading the result. When ``timeout`` is None, read the
    current state without waiting. Raises if the task itself failed.
    """
    result = AsyncResult(task_id, app=celery_app)
    if timeout is None:
        if not result.ready():
            return f"Task {task_id} is not ready (status: {result.status})"
        return result.get()
    try:
        return result.get(timeout=timeout)
    except CeleryTimeoutError:
        return f"Task {task_id} is not ready (status: {result.status})"


@tai42_app.tools.tool(tags={"backend"})
def backend_cancel_task(task_id: str, terminate: bool = False) -> str:
    """Revoke (cancel) a running or queued task. Optionally terminate if running."""
    result = AsyncResult(task_id, app=celery_app)
    result.revoke(terminate=terminate)
    return f"Task {task_id} revoked (terminate={terminate})"


@tai42_app.tools.tool(tags={"backend"})
def backend_active_tasks(worker_name: str | None = None) -> dict[str, Any]:
    """Get all currently executing tasks on the worker(s).

    Returns Celery's inspect shape: a mapping of worker name to that worker's
    list of task-info dicts.
    """
    inspect = celery_app.control.inspect([worker_name] if worker_name else None)
    return inspect.active() or {}


@tai42_app.tools.tool(tags={"backend"})
def backend_reserved_tasks(worker_name: str | None = None) -> dict[str, Any]:
    """Get all reserved tasks (prefetched by a worker, not yet started).

    Returns Celery's inspect shape: a mapping of worker name to that worker's
    list of task-info dicts.
    """
    inspect = celery_app.control.inspect([worker_name] if worker_name else None)
    return inspect.reserved() or {}


@tai42_app.tools.tool(tags={"backend"})
def backend_scheduled_tasks(worker_name: str | None = None) -> dict[str, Any]:
    """Get all scheduled tasks (waiting for ETA/countdown).

    Returns Celery's inspect shape: a mapping of worker name to that worker's
    list of scheduled-task dicts (each carries the ``eta`` and the request).
    """
    inspect = celery_app.control.inspect([worker_name] if worker_name else None)
    return inspect.scheduled() or {}


@tai42_app.tools.tool(tags={"backend"})
def backend_registered_tasks(worker_name: str | None = None) -> dict[str, Any]:
    """Get all registered task names available to a worker."""
    inspect = celery_app.control.inspect([worker_name] if worker_name else None)
    return inspect.registered() or {}


@tai42_app.tools.tool(tags={"backend"})
def backend_worker_stats(worker_name: str | None = None) -> dict:
    """Get worker statistics and info.

    Returns Celery's inspect shape: a mapping of worker name to that worker's
    stats dict (pool, totals, broker, and process info).
    """
    inspect = celery_app.control.inspect([worker_name] if worker_name else None)
    return inspect.stats() or {}


@tai42_app.tools.tool(tags={"backend"})
def backend_worker_queues(worker_name: str | None = None) -> dict:
    """List all queues each worker is consuming from."""
    inspect = celery_app.control.inspect([worker_name] if worker_name else None)
    return inspect.active_queues() or {}


@tai42_app.tools.tool(tags={"backend"})
def backend_list_failed_tasks() -> list:
    """List failed tasks.

    Celery does not keep a queryable index of failed tasks, so this backend
    cannot implement this capability.
    """
    raise NotImplementedError("backend 'celery' does not support backend_list_failed_tasks")


@tai42_app.tools.tool(tags={"backend"})
async def backend_delete_schedule(name: str) -> dict[str, Any]:
    """Remove a periodic task from RedBeat by its unique name."""
    settings = celery_settings()
    async with _redbeat_redis() as r:
        key = settings.redbeat_task_key(name)
        # Zset member first, then the entry hash (RedBeat's delete order); the
        # reverse would transiently leave a dangling member with no backing hash.
        await r.zrem(settings.redbeat_schedule_key, key)
        await r.delete(key)
        return {"status": "deleted", "name": name}


@tai42_app.tools.tool(tags={"backend"})
async def backend_list_schedules() -> list[dict[str, Any]]:
    """List all RedBeat schedule entries as ``ScheduleListRow`` dumps.

    Each row carries ``name``, the fired ``tool`` and the ``cadence`` (read from the
    entry's ``definition`` hash and mapped from RedBeat's shape), ``enabled``,
    ``next_run_at_ts`` and ``next_run_at_iso`` (the zset score) — never the stored
    arguments. The listing iterates the schedule zset, so a disabled
    entry removed from the zset does not appear. Zset members are entry keys
    (``redbeat:{name}``); the reported ``name`` is the bare schedule name,
    matching what the other schedule tools accept and what
    ``backend_export_schedules`` emits. Each entry's ``definition`` and zset
    membership are read atomically: a member observed with no backing
    ``definition`` raises loudly rather than being listed half-shaped, while a
    member gone again at that same instant means the entry was deleted after
    the listing snapshot and its absence is the correct current state, so the
    row is omitted.
    """
    settings = celery_settings()
    async with _redbeat_redis() as r:
        items = await r.zrange(settings.redbeat_schedule_key, 0, -1, withscores=True)
        out = []
        for member, score in items:
            member_name = _text(member)
            key = settings.redbeat_task_key(member_name)
            definition, member_score = await _read_definition_and_membership(r, settings.redbeat_schedule_key, key)
            if not definition:
                if member_score is None:
                    # Member and hash both gone: deleted after the zrange snapshot.
                    continue
                raise KeyError(f"Schedule zset lists {member_name!r} but hash {key!r} has no 'definition'")
            def_obj = json.loads(_text(definition))
            ts = float(score)
            name = member_name.removeprefix(settings.redbeat_key_prefix)
            out.append(
                ScheduleListRow(
                    name=name,
                    tool=_fired_tool(name, dict(def_obj.get("kwargs") or {})),
                    cadence=from_redbeat(def_obj["schedule"]),
                    enabled=bool(def_obj.get("enabled", True)),
                    next_run_at_ts=ts,
                    next_run_at_iso=datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts > 0 else None,
                ).model_dump(mode="json")
            )
        return out


@tai42_app.tools.tool(tags={"backend"})
async def backend_get_schedule(name: str) -> dict[str, Any]:
    """Get RedBeat schedule 'definition' + 'meta' and current zset score for next run."""
    settings = celery_settings()
    async with _redbeat_redis() as r:
        key = settings.redbeat_task_key(name)

        definition = await r.hget(key, "definition")
        meta = await r.hget(key, "meta")
        if not definition:
            return {"status": "not_found", "name": name}

        def_txt = _text(definition)
        meta_txt = _text(meta)

        try:
            def_obj = json.loads(def_txt)
        except json.JSONDecodeError:
            def_obj = {"_raw": def_txt}

        meta_obj = {}
        if meta_txt:
            try:
                meta_obj = json.loads(meta_txt)
            except json.JSONDecodeError:
                meta_obj = {"_raw": meta_txt}

        score = await r.zscore(settings.redbeat_schedule_key, key)
        ts = float(score) if score is not None else None

        return {
            "name": name,
            "definition": def_obj,
            "meta": meta_obj,
            "next_run_at_ts": ts,
            "next_run_at_iso": (datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts is not None else None),
            "redis_key": key,
        }


@tai42_app.tools.tool(tags={"backend"})
async def backend_enable_schedule(name: str) -> dict[str, Any]:
    """Enable a RedBeat schedule (sets definition.enabled=True and ensures it's in the zset)."""
    settings = celery_settings()
    async with _redbeat_redis() as r:
        key = settings.redbeat_task_key(name)

        definition = await r.hget(key, "definition")
        if not definition:
            return {"status": "not_found", "name": name}

        defn = json.loads(_text(definition))
        defn["enabled"] = True
        await r.hset(key, "definition", json.dumps(defn))
        await r.zadd(settings.redbeat_schedule_key, {key: 0})  # run ASAP; Beat reschedules next tick
        return {"status": "enabled", "name": name}


@tai42_app.tools.tool(tags={"backend"})
async def backend_disable_schedule(name: str, remove_from_queue: bool = True) -> dict[str, Any]:
    """Disable a RedBeat schedule (sets definition.enabled=False). Optionally remove from zset."""
    settings = celery_settings()
    async with _redbeat_redis() as r:
        key = settings.redbeat_task_key(name)

        definition = await r.hget(key, "definition")
        if not definition:
            return {"status": "not_found", "name": name}

        defn = json.loads(_text(definition))
        defn["enabled"] = False
        await r.hset(key, "definition", json.dumps(defn))
        if remove_from_queue:
            await r.zrem(settings.redbeat_schedule_key, key)
        return {"status": "disabled", "name": name}


@tai42_app.tools.tool(tags={"backend"})
async def backend_run_schedule_now(name: str) -> dict[str, Any]:
    """Force a schedule to run ASAP by setting its zset score to 0 (Beat picks it up next tick).

    A name with no backing entry is ``not_found`` — queueing it anyway would
    report success while Beat drops the dangling zset member without running
    anything.
    """
    settings = celery_settings()
    async with _redbeat_redis() as r:
        key = settings.redbeat_task_key(name)
        if not await r.exists(key):
            return {"status": "not_found", "name": name}
        added = await r.zadd(settings.redbeat_schedule_key, {key: 0})
        return {"status": "queued", "name": name, "zadd_result": int(added)}


@tai42_app.tools.tool(tags={"backend"})
async def backend_schedule_exists(name: str) -> bool:
    """Return True if a RedBeat schedule entry exists."""
    settings = celery_settings()
    async with _redbeat_redis() as r:
        return bool(await r.exists(settings.redbeat_task_key(name)))


@tai42_app.tools.tool(tags={"backend"})
async def backend_update_schedule(
    name: str,
    new_schedule: int | float | str | dict[str, Any] | None = None,
    next_run_in_ms: int | None = None,
    next_run_at_ts: float | None = None,
) -> dict[str, Any]:
    """Update an existing RedBeat entry.

    - `new_schedule` (optional): int/float (seconds interval), str (5-field crontab), or a
      friendly/``kind``-tagged dict, parsed into a cadence. If omitted, the schedule is left unchanged.
    - `next_run_in_ms` (optional): delay in milliseconds from *now* (UTC).
    - `next_run_at_ts` (optional): absolute UNIX timestamp (UTC). Ignored if `next_run_in_ms` is provided.
    - If neither `new_schedule` nor `next_run_*` is given → returns {"status": "skipped"}.

    Returns: dict with status, previous_schedule and new_schedule (each a cadence dump),
    next_run_at_ts/iso, redis_key.
    """
    settings = celery_settings()
    async with _redbeat_redis() as r:
        key = settings.redbeat_task_key(name)
        schedule_zset = settings.redbeat_schedule_key

        if not await r.exists(key):
            return {"status": "not_found", "name": name, "message": "Schedule entry does not exist; nothing updated."}

        raw_def = await r.hget(key, "definition")
        if not raw_def:
            return {"status": "skipped", "name": name, "message": "Existing entry has no 'definition'."}

        try:
            defn = json.loads(_text(raw_def)) or {}
        except json.JSONDecodeError:
            return {"status": "skipped", "name": name, "message": "Existing definition is not valid JSON."}

        prev_schedule = from_redbeat(defn["schedule"]).model_dump(mode="json")
        new_cadence = None
        updated = False

        if new_schedule is not None:
            new_cadence = normalize_schedule(new_schedule)
            defn["schedule"] = to_redbeat(new_cadence)
            await r.hset(key, "definition", json.dumps(defn))
            updated = True

        if next_run_in_ms is not None:
            next_run_at_ts = datetime.now(tz=UTC).timestamp() + next_run_in_ms / 1000.0
        if next_run_at_ts is not None:
            await r.zadd(schedule_zset, {key: float(next_run_at_ts)})
            updated = True

        if not updated:
            return {"status": "skipped", "name": name, "message": "No schedule or next run provided; nothing updated."}

        score = await r.zscore(schedule_zset, key)
        ts = float(score) if score is not None else None

        return {
            "status": "updated",
            "name": name,
            "previous_schedule": prev_schedule,
            "new_schedule": new_cadence.model_dump(mode="json") if new_cadence is not None else prev_schedule,
            "next_run_at_ts": ts,
            "next_run_at_iso": (datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts is not None else None),
            "redis_key": key,
        }


@tai42_app.tools.tool(tags={"backend"})
async def backend_export_schedules() -> list[dict[str, Any]]:
    """Export every RedBeat schedule as a portable list of ``ScheduleRecord`` dumps.

    Unions the schedule zset members with every entry hash found by the
    ``redbeat:*`` key pattern, reads each entry's full 'definition', and captures
    its name, the fired ``tool`` (split out of the stored kwargs' tool-name key; an
    entry without it raises), args, the remaining kwargs, the cadence (mapped from
    RedBeat's shape), and enabled flag. The zset holds only ENABLED entries, so a schedule disabled with
    the default ``remove_from_queue=True`` keeps its ``redbeat:{name}`` hash but
    leaves the zset; the pattern scan re-includes it (it is re-enable-able, a live
    schedule) where a zset-only walk would silently drop it. Runtime-only next-run
    scores and meta are left out so the result round-trips back through
    ``backend_import_schedules`` unchanged. Each entry's ``definition`` and zset
    membership are read atomically: a member observed with no backing
    ``definition`` raises loudly, while a member gone again at that same instant
    means the entry was deleted after the snapshot and is omitted as the correct
    current state.
    """
    settings = celery_settings()
    async with _redbeat_redis() as r:
        members = await r.zrange(settings.redbeat_schedule_key, 0, -1)
        # RedBeat's own keys live under the double-colon ``redbeat::`` namespace (the
        # schedule zset, lock, statics) — skip them so only entry hashes are read.
        internal_prefix = f"{settings.redbeat_key_prefix}:"
        entry_keys: list[str] = []
        seen: set[str] = set()
        for member in members:
            key = _text(member)
            if key not in seen:
                seen.add(key)
                entry_keys.append(key)
        async for scanned in r.scan_iter(match=f"{settings.redbeat_key_prefix}*"):
            key = _text(scanned)
            if key.startswith(internal_prefix) or key in seen:
                continue
            seen.add(key)
            entry_keys.append(key)

        records: list[dict[str, Any]] = []
        for key in entry_keys:
            definition, member_score = await _read_definition_and_membership(r, settings.redbeat_schedule_key, key)
            if not definition:
                if member_score is None:
                    # Hash and zset member both gone: deleted after the snapshot.
                    continue
                raise KeyError(f"Schedule zset lists {key!r} but hash {key!r} has no 'definition'")

            def_obj = json.loads(_text(definition))
            kwargs = dict(def_obj.get("kwargs") or {})
            tool = _fired_tool(def_obj["name"], kwargs)
            record = ScheduleRecord(
                name=def_obj["name"],
                tool=tool,
                args=def_obj.get("args") or [],
                kwargs=kwargs,
                cadence=from_redbeat(def_obj["schedule"]),
                enabled=bool(def_obj["enabled"]),
            )
            records.append(record.model_dump(mode="json"))
        return records


@tai42_app.tools.tool(tags={"backend"})
async def backend_import_schedules(
    schedules: list[dict[str, Any]], mode: ScheduleImportMode = "skip"
) -> dict[str, Any]:
    """Recreate schedules produced by ``backend_export_schedules`` as RedBeat entries.

    Each row is validated as a ``ScheduleRecord``, its ``tool`` injected back under the
    tool-name key, and saved via a ``RedBeatSchedulerEntry`` exactly as the create
    extension does. A new name is created (counted 'created'); scheduling is recomputed
    on save, so no stale next-run time is persisted.

    ``mode`` keys an existing ``redbeat:{name}`` entry: under ``skip`` (the default)
    an existing name is left untouched, never re-applied, and counted in
    ``skipped_existing``; under ``overwrite`` the existing entry is replaced in place
    (counted 'updated'). This is the per-record import mode the backup restore
    forwards across the tool boundary, honored here as every backend honors it.

    Returns a ``ScheduleImportReport`` dump; each error row is ``{"index", "name",
    "error"}``. A row that fails to validate or save is reported there loudly, never
    silently dropped. ``skipped`` is always 0 — this backend has no per-row skip case
    (a disabled record persists as a disabled RedBeat entry).
    """
    settings = celery_settings()
    created = 0
    updated = 0
    skipped = 0
    skipped_existing = 0
    errors: list[ScheduleImportError] = []

    async with _redbeat_redis() as r:
        for index, entry in enumerate(schedules):
            raw_name = entry.get("name") if isinstance(entry, dict) else None
            entry_name = raw_name if isinstance(raw_name, str) else None
            try:
                record = ScheduleRecord.model_validate(entry)
                key = settings.redbeat_task_key(record.name)
                exists = bool(await r.exists(key))

                if exists and mode == "skip":
                    # An existing schedule (keyed by name) is left in place.
                    skipped_existing += 1
                    continue

                rb_entry = RedBeatSchedulerEntry(
                    name=record.name,
                    task="celery.tool_execution",
                    schedule=to_celery_schedule(record.cadence),
                    args=record.args,
                    kwargs={**record.kwargs, settings.tool_name_arg: record.tool},
                    enabled=record.enabled,
                    app=celery_app,
                )
                await asyncio.to_thread(rb_entry.save)

                if exists:
                    updated += 1
                else:
                    created += 1
            except Exception as e:
                errors.append(ScheduleImportError(index=index, name=entry_name, error=repr(e)))

    return ScheduleImportReport(
        created=created, updated=updated, skipped=skipped, skipped_existing=skipped_existing, errors=errors
    ).model_dump()
