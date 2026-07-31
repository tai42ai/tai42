"""The ``backend_*`` tool surface over the arq broker.

Task/worker tools use arq's public ``Job`` API and pool queries; schedule tools
operate on this backend's own schedule hashes. Capabilities arq has no reliable
data model for raise ``NotImplementedError``. The host's schedule marker tools
are ``backend_list_schedules`` / ``backend_delete_schedule`` /
``backend_export_schedules`` / ``backend_import_schedules``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import orjson
from arq.constants import in_progress_key_prefix
from arq.jobs import Job, JobStatus, ResultNotFound
from arq.utils import timestamp_ms
from tai42_contract.app import tai42_app
from tai42_kit.utils.runtime.schedule_util import normalize_schedule

from tai42_backend_arq.pool import RedisPoolManager
from tai42_backend_arq.records import (
    ScheduleRecord,
    derive_cron_or_interval,
    next_run_after,
    parse_cron_or_interval,
)
from tai42_backend_arq.scheduler import (
    abort_job,
    abort_schedule_task,
    safe_schedule_transition,
    schedule_lock,
    wait_job_result,
)
from tai42_backend_arq.settings import TaskFailedError, arq_settings, job_deserializer

logger = logging.getLogger(__name__)


def _failure_detail(result: Any) -> str:
    """Human-readable detail of a stored failure result: the revived failure's
    ``repr``, or the ``repr`` of whatever else was stored."""
    if isinstance(result, TaskFailedError):
        return result.error_repr
    if isinstance(result, asyncio.CancelledError):
        return str(result) or repr(result)
    return repr(result)


@tai42_app.tools.tool(tags={"backend"})
async def backend_task_status(task_id: str) -> str:
    """
    Return the current status of a given task ID.
    """
    arq_redis: Any = await RedisPoolManager.get()
    job = Job(task_id, arq_redis, _deserializer=job_deserializer)
    status = await job.status()
    return status.value if status else "unknown"


@tai42_app.tools.tool(tags={"backend"})
async def backend_task_result(task_id: str, timeout: float | None = None) -> Any:
    """
    Return the result of a completed task by ID.

    - `timeout is None`: return the current state without waiting. If the task is
      not yet complete, a "not ready" snapshot value is returned.
    - `timeout` given: wait up to that many seconds for the task to complete, then
      return its result. If the wait elapses before completion, a "not ready"
      value is returned (consistent with the no-wait path).

    If the task does not exist, or completed but its result is no longer retained,
    a clear "not found" / "no result" value is returned. If the task raised an
    exception, the stored failure re-raises here as ``TaskFailedError`` carrying
    the original exception's type name, ``repr``, and traceback text (an aborted
    task re-raises the same way, its detail naming the ``CancelledError``). A
    SUCCESSFUL result that could not be JSON-serialized is returned as its
    stored tagged description (type name and ``repr``) instead of the value.
    """
    arq_redis: Any = await RedisPoolManager.get()
    job = Job(task_id, arq_redis, _deserializer=job_deserializer)
    status = await job.status()
    if status == JobStatus.not_found:
        return f"Task {task_id} not found"
    if timeout is None and status != JobStatus.complete:
        return f"Task {task_id} is not ready (status: {status})"
    try:
        return await wait_job_result(job, timeout=timeout)
    except TimeoutError:
        status = await job.status()
        return f"Task {task_id} is not ready (status: {status})"
    except ResultNotFound:
        return f"No result found for task {task_id}"


@tai42_app.tools.tool(tags={"backend"})
async def backend_cancel_task(task_id: str) -> str:
    """
    Cancel (abort) a running or queued task.

    Waits up to the configured task timeout for an outcome. No outcome within
    the wait is reported as requested-but-unconfirmed (the request stays
    recorded and the worker honors it at pick-up). A task that reaches a
    stored outcome during the wait is reported from that outcome, never as a
    cancel failure: ``Job.abort`` polls the result key while it waits, and a
    stored abort replays out of the poll as its revived ``CancelledError`` —
    arq's confirmed-abort verdict, reported as "aborted" — while a stored
    own-failure replays as its revived ``TaskFailedError``, reported as
    failed-on-its-own with the stored detail. A stored success (or a task
    vanishing without a retained result) reads back as a plain ``False``. Only
    a stored failure carrying no revivable detail (arq's last-ditch placeholder
    when even the tagged description could not serialize) stays reported as
    aborted-or-failed, with the raw stored value appended.
    """
    arq_redis: Any = await RedisPoolManager.get()
    job = Job(task_id, arq_redis, _deserializer=job_deserializer)
    status = await job.status()
    if status in (JobStatus.not_found, JobStatus.complete):
        return f"Task {task_id} cannot be canceled (status: {status})"
    try:
        aborted = await abort_job(job, timeout=arq_settings().task_timeout)
    except TimeoutError:
        return f"Task {task_id} abort requested but not confirmed"
    except Exception:
        status = await job.status()
        if status == JobStatus.complete:
            # Replayed stored outcome of a task that finished during the wait —
            # not an abort failure. A revived TaskFailedError is the task's own
            # failure; a failure with no revivable detail leaves abort and
            # own-failure indistinguishable.
            info = await job.result_info()
            if info is not None and not info.success:
                detail = _failure_detail(info.result)
                if isinstance(info.result, TaskFailedError):
                    return f"Task {task_id} failed on its own before the abort could take effect: {detail}"
                return (
                    f"Task {task_id} finished in failure after the abort request "
                    f"(aborted or failed on its own): {detail}"
                )
            return f"Task {task_id} cannot be canceled (status: {status})"
        if status == JobStatus.not_found:
            # Vanished between replay and re-check; gone, guaranteed not to run.
            return f"Task {task_id} cannot be canceled (status: {status})"
        raise
    if aborted:
        # ``Job.abort``'s confirmed-abort verdict (a replayed CancelledError).
        return f"Task {task_id} aborted"
    status = await job.status()
    if status in (JobStatus.not_found, JobStatus.complete):
        # abort returned False on a replayed success / no retained result:
        # the task finished during the wait, nothing left to cancel.
        return f"Task {task_id} cannot be canceled (status: {status})"
    return f"Task {task_id} abort requested but not confirmed"


@tai42_app.tools.tool(tags={"backend"})
async def backend_active_tasks() -> dict[str, Any]:
    """
    Get all currently executing tasks (in-progress jobs).

    Returns a flat mapping of job id to ``{"status": "in_progress"}`` — arq has
    no per-worker attribution for a running job, so the map is keyed by job id,
    not by worker.
    """
    arq_redis: Any = await RedisPoolManager.get()
    # arq marks a running job with an in-progress key; the job's status API is
    # the authority (a lingering key after completion reads complete).
    pattern = f"{in_progress_key_prefix}*".encode()
    job_ids: list[str] = []
    async for key in arq_redis.scan_iter(match=pattern):
        raw = key.decode() if isinstance(key, bytes) else key
        job_ids.append(raw[len(in_progress_key_prefix) :])
    tasks: dict[str, Any] = {}
    for job_id in job_ids:
        status = await Job(job_id, arq_redis, _deserializer=job_deserializer).status()
        if status == JobStatus.in_progress:
            tasks[job_id] = {"status": status.value}
    return tasks


@tai42_app.tools.tool(tags={"backend"})
async def backend_reserved_tasks() -> list[str]:
    """
    Get all reserved/queued tasks (due to run, not yet picked up).

    Returns a flat list of job ids — arq has one queue and no per-worker
    reservation, so there is no worker or queue keying to report.
    """
    arq_redis: Any = await RedisPoolManager.get()
    now_ms = timestamp_ms()
    return [
        job.job_id
        for job in await arq_redis.queued_jobs()
        if job.job_id is not None and job.score is not None and job.score <= now_ms
    ]


@tai42_app.tools.tool(tags={"backend"})
async def backend_scheduled_tasks() -> dict[str, float]:
    """
    Get all scheduled/deferred tasks (future-dated queue entries).

    Returns a flat mapping of job id to its due time in milliseconds since the
    epoch (the queue zset score).
    """
    arq_redis: Any = await RedisPoolManager.get()
    now_ms = timestamp_ms()
    return {
        job.job_id: float(job.score)
        for job in await arq_redis.queued_jobs()
        if job.job_id is not None and job.score is not None and job.score > now_ms
    }


@tai42_app.tools.tool(tags={"backend"})
async def backend_list_failed_tasks() -> list[dict[str, Any]]:
    """
    Get the failed (including aborted) tasks whose results are still retained,
    as ``{"task_id", "error"}`` rows — ``error`` carries the stored failure
    detail (the original exception's ``repr``; an aborted task's detail names
    its ``CancelledError``). arq keeps job outcomes only for the configured
    keep-result window, so this lists failures within that window.
    """
    arq_redis: Any = await RedisPoolManager.get()
    return [
        {"task_id": result.job_id, "error": _failure_detail(result.result)}
        for result in await arq_redis.all_job_results()
        if not result.success and result.job_id is not None
    ]


@tai42_app.tools.tool(tags={"backend"})
async def backend_list_schedules() -> list[dict[str, Any]]:
    """
    List all custom schedules.

    Each row carries the canonical keys ``name``, ``enabled``,
    ``next_run_at_ts`` and ``next_run_at_iso`` (the pending job's due time,
    read from the schedule hash; null when none is recorded yet), plus the
    backend-specific extras ``schedule`` (the canonical schedule dict),
    ``target``, ``args``, and ``kwargs``.
    """
    arq_redis: Any = await RedisPoolManager.get()
    settings = arq_settings()
    schedule_keys = [key async for key in arq_redis.scan_iter(match=settings.arq_schedule_pattern.encode())]
    schedules = []
    for key in schedule_keys:
        raw = key.decode() if isinstance(key, bytes) else key
        name = raw.split(":")[-1]
        data = await arq_redis.hgetall(raw)
        if not data:
            # Empty hash: the schedule was deleted between the scan and this read.
            continue
        next_run_raw = data.get(b"last_scheduled_ts")
        next_run_ts = float(next_run_raw) if next_run_raw else None
        schedules.append(
            {
                "name": name,
                "enabled": data.get(b"enabled", b"true").decode() == "true",
                "next_run_at_ts": next_run_ts,
                "next_run_at_iso": (datetime.fromtimestamp(next_run_ts, tz=UTC).isoformat() if next_run_ts else None),
                "schedule": orjson.loads(data.get(b"schedule", b"{}")),
                "target": data.get(b"target", b"").decode(),
                "args": orjson.loads(data.get(b"args", b"[]")),
                "kwargs": orjson.loads(data.get(b"kwargs", b"{}")),
            }
        )
    return schedules


@tai42_app.tools.tool(tags={"backend"})
async def backend_export_schedules() -> list[dict[str, Any]]:
    """
    Export every custom schedule as a list of portable, JSON-serializable records.

    Each record captures only the durable definition of a schedule -- its name,
    positional args, keyword args (which carry the tool name verbatim), the
    canonical interval-or-crontab schedule dict, and whether it is enabled.
    Runtime-only fields (job_id, last_scheduled_ts, aborted, cron_or_interval)
    are omitted because they are derivable from the schedule at import time. The
    returned list round-trips through ``backend_import_schedules``. A schedule
    hash with a missing or malformed stored schedule dict raises loudly — an
    export must never emit a record that cannot recreate its schedule.
    """
    arq_redis: Any = await RedisPoolManager.get()
    settings = arq_settings()
    schedule_keys = [key async for key in arq_redis.scan_iter(match=settings.arq_schedule_pattern.encode())]
    records = []
    for key in schedule_keys:
        raw = key.decode() if isinstance(key, bytes) else key
        name = raw.split(":")[-1]
        data = await arq_redis.hgetall(raw)
        if not data:
            # Empty hash: the schedule was deleted between the scan and this read.
            continue
        raw_schedule = data.get(b"schedule")
        if raw_schedule is None:
            raise ValueError(f"schedule '{name}' has no stored schedule definition; cannot export it")
        record = ScheduleRecord(
            name=name,
            args=orjson.loads(data.get(b"args", b"[]")),
            kwargs=orjson.loads(data.get(b"kwargs", b"{}")),
            schedule=orjson.loads(raw_schedule),
            enabled=data.get(b"enabled", b"true").decode() == "true",
        )
        records.append(record)
    return [record.model_dump() for record in records]


@tai42_app.tools.tool(tags={"backend"})
async def backend_import_schedules(schedules: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Import schedules previously produced by ``backend_export_schedules``.

    Each entry is parsed as a ``ScheduleRecord`` and written through the same
    low-level schedule-hash mapping the create path uses: target is set to
    ``tool_execution`` and the args, kwargs, canonical schedule, derived
    cron_or_interval and enabled flag are stored under ``arq:schedule:{name}``.
    The next run is recomputed from the schedule (never taken from a stale
    exported value).

    Imports are idempotent by name: an existing name is overwritten and counted
    as ``updated``; a new name is counted as ``created``. A malformed entry or a
    failed write is recorded in ``errors`` -- never silently dropped. Returns
    ``{"created": int, "updated": int, "skipped": int, "errors": [...]}``; each
    error row is ``{"index", "name", "error"}``. ``skipped`` is always 0 --
    this backend has no skip case (every valid record can be stored, enabled
    or disabled).
    """
    arq_redis: Any = await RedisPoolManager.get()
    settings = arq_settings()

    created = 0
    updated = 0
    skipped = 0
    errors: list[dict[str, Any]] = []

    for index, entry in enumerate(schedules):
        try:
            record = ScheduleRecord.model_validate(entry)

            norm = normalize_schedule(record.schedule)
            cron_or_interval = derive_cron_or_interval(norm)
            defer_by, last_scheduled_ts = next_run_after(cron_or_interval, datetime.now(UTC))

            key = settings.arq_schedule_key(record.name)
            exists = bool(await arq_redis.exists(key))

            mapping_updates = {
                "target": "tool_execution",
                "args": orjson.dumps(record.args),
                "kwargs": orjson.dumps(record.kwargs),
                "schedule": orjson.dumps(norm),
                "cron_or_interval": str(cron_or_interval),
                "enabled": "true" if record.enabled else "false",
            }

            await safe_schedule_transition(
                arq_redis,
                record.name,
                defer_by=defer_by,
                last_scheduled_ts=last_scheduled_ts,
                mapping_updates=mapping_updates,
                enforce_job_id=None,
            )

            if exists:
                updated += 1
            else:
                created += 1
        except Exception as exc:
            name = entry.get("name") if isinstance(entry, dict) else None
            errors.append({"index": index, "name": name, "error": repr(exc)})

    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


@tai42_app.tools.tool(tags={"backend"})
async def backend_get_schedule(name: str) -> dict[str, Any]:
    """
    Get details of a custom schedule.
    """
    arq_redis: Any = await RedisPoolManager.get()
    settings = arq_settings()
    key = settings.arq_schedule_key(name)
    data = await arq_redis.hgetall(key)
    if not data:
        # Empty hash means no schedule.
        return {"status": "not_found"}
    return {
        "enabled": data.get(b"enabled", b"true").decode() == "true",
        "schedule": orjson.loads(data.get(b"schedule", b"{}")),
        "target": data.get(b"target", b"").decode(),
        "args": orjson.loads(data.get(b"args", b"[]")),
        "kwargs": orjson.loads(data.get(b"kwargs", b"{}")),
    }


@tai42_app.tools.tool(tags={"backend"})
async def backend_delete_schedule(name: str) -> dict[str, Any]:
    """
    Delete a custom schedule.

    Runs under the per-schedule lock so the delete serializes with schedule
    transitions and flag writes — a transition mid-flight can never re-write
    the hash after this delete removed it.
    """
    arq_redis: Any = await RedisPoolManager.get()
    settings = arq_settings()
    key = settings.arq_schedule_key(name)
    async with schedule_lock(arq_redis, name):
        if not await arq_redis.exists(key):
            return {"status": "not_found", "name": name}

        await abort_schedule_task(key)
        await arq_redis.delete(key)

    return {"status": "deleted", "name": name}


@tai42_app.tools.tool(tags={"backend"})
async def backend_enable_schedule(name: str) -> dict[str, Any]:
    """
    Enable a custom schedule.

    The flag write runs under the per-schedule lock, after an existence check
    that stays true for the write — writing the flag into a hash a concurrent
    delete just removed would resurrect the schedule as a partial hash.
    """
    arq_redis: Any = await RedisPoolManager.get()
    settings = arq_settings()
    key = settings.arq_schedule_key(name)
    async with schedule_lock(arq_redis, name):
        if not await arq_redis.exists(key):
            return {"status": "not_found"}
        await arq_redis.hset(key, "enabled", "true")
    return {"status": "enabled"}


@tai42_app.tools.tool(tags={"backend"})
async def backend_disable_schedule(name: str) -> dict[str, Any]:
    """
    Disable a custom schedule.

    The flag write runs under the per-schedule lock, after an existence check
    that stays true for the write — writing the flag into a hash a concurrent
    delete just removed would resurrect the schedule as a partial hash.
    """
    arq_redis: Any = await RedisPoolManager.get()
    settings = arq_settings()
    key = settings.arq_schedule_key(name)
    async with schedule_lock(arq_redis, name):
        if not await arq_redis.exists(key):
            return {"status": "not_found"}
        await arq_redis.hset(key, "enabled", "false")
    return {"status": "disabled"}


@tai42_app.tools.tool(tags={"backend"})
async def backend_run_schedule_now(name: str) -> dict[str, Any]:
    """
    Force a schedule to run ASAP.
    """
    arq_redis: Any = await RedisPoolManager.get()
    settings = arq_settings()
    key = settings.arq_schedule_key(name)
    data = await arq_redis.hgetall(key)
    if not data:
        # Empty hash means no schedule.
        return {"status": "not_found"}

    target = data.get(b"target", b"").decode()

    args = orjson.loads(data.get(b"args", b"[]"))
    kwargs = orjson.loads(data.get(b"kwargs", b"{}"))

    await arq_redis.enqueue_job(target, *args, **kwargs)
    return {"status": "queued"}


@tai42_app.tools.tool(tags={"backend"})
async def backend_schedule_exists(name: str) -> bool:
    """
    Return True if a custom schedule entry exists.
    """
    arq_redis: Any = await RedisPoolManager.get()
    settings = arq_settings()
    key = settings.arq_schedule_key(name)
    return bool(await arq_redis.exists(key))


@tai42_app.tools.tool(tags={"backend"})
async def backend_update_schedule(
    name: str,
    new_schedule: int | float | str | dict[str, Any] | None = None,
    next_run_in_ms: int | None = None,
    next_run_at_ts: float | None = None,
) -> dict[str, Any]:
    """
    Update an existing custom schedule entry.

    - `new_schedule` (optional): int/float (seconds interval), str (5-field crontab), or dict.
      If omitted, the schedule is left unchanged.
    - `next_run_in_ms` (optional): delay in milliseconds from *now* (UTC). For initial enqueue.
    - `next_run_at_ts` (optional): absolute UNIX timestamp (UTC). Ignored if `next_run_in_ms` is provided.
    - If neither `new_schedule` nor `next_run_*` is given → returns {"status": "skipped"}.

    Returns: dict with status, previous_schedule, new_schedule, next_run_at_ts/iso, redis_key.
    """
    arq_redis: Any = await RedisPoolManager.get()
    settings = arq_settings()
    key = settings.arq_schedule_key(name)
    if not await arq_redis.exists(key):
        return {
            "status": "not_found",
            "name": name,
            "message": "Schedule entry does not exist; nothing updated.",
            "redis_key": key,
        }

    data = await arq_redis.hgetall(key)
    prev_schedule = orjson.loads(data.get(b"schedule", b"{}"))
    prev_cron_or_interval = parse_cron_or_interval(data.get(b"cron_or_interval", b"").decode("utf-8"))

    updated = False
    cron_or_interval = prev_cron_or_interval
    mapping_updates: dict[str, Any] = {}

    if new_schedule is not None:
        norm = normalize_schedule(new_schedule)
        cron_or_interval = derive_cron_or_interval(norm)
        mapping_updates["schedule"] = orjson.dumps(norm)
        mapping_updates["cron_or_interval"] = str(cron_or_interval)
        updated = True

    if next_run_in_ms is not None:
        next_run_at_ts = datetime.now(tz=UTC).timestamp() + next_run_in_ms / 1000.0

    if next_run_at_ts is not None or new_schedule is not None:
        now = datetime.now(UTC)
        if next_run_at_ts is not None:
            defer_by = next_run_at_ts - now.timestamp()
            last_scheduled_ts = next_run_at_ts
        else:
            defer_by, last_scheduled_ts = next_run_after(cron_or_interval, now)
            next_run_at_ts = last_scheduled_ts

        next_job = await safe_schedule_transition(
            arq_redis,
            name,
            defer_by=defer_by,
            last_scheduled_ts=last_scheduled_ts,
            mapping_updates=mapping_updates,
            enforce_job_id=None,
        )
        if next_job is None:
            # Hash gone: a concurrent delete removed the schedule after the
            # exists-check, and the update mapping must not recreate it.
            return {
                "status": "not_found",
                "name": name,
                "message": "Schedule was deleted concurrently; nothing updated.",
                "redis_key": key,
            }
        updated = True

    if not updated:
        return {
            "status": "skipped",
            "name": name,
            "message": "No changes provided; nothing updated.",
            "redis_key": key,
        }

    new_data = await arq_redis.hgetall(key)
    stored_schedule = orjson.loads(new_data.get(b"schedule", b"{}"))

    return {
        "status": "updated",
        "name": name,
        "previous_schedule": prev_schedule,
        "new_schedule": stored_schedule,
        "next_run_at_ts": next_run_at_ts,
        "next_run_at_iso": datetime.fromtimestamp(next_run_at_ts, tz=UTC).isoformat() if next_run_at_ts else None,
        "redis_key": key,
    }


@tai42_app.tools.tool(tags={"backend"})
async def backend_registered_tasks() -> list[str]:
    """
    List the task functions registered with the worker.
    """
    raise NotImplementedError("backend 'arq' does not support backend_registered_tasks")


@tai42_app.tools.tool(tags={"backend"})
async def backend_worker_stats() -> dict[str, Any]:
    """
    Return runtime statistics for the worker.
    """
    raise NotImplementedError("backend 'arq' does not support backend_worker_stats")


@tai42_app.tools.tool(tags={"backend"})
async def backend_worker_queues() -> list[str]:
    """
    List the queues the worker consumes from.
    """
    raise NotImplementedError("backend 'arq' does not support backend_worker_queues")


@tai42_app.tools.tool(tags={"backend"})
async def backend_ping_worker() -> dict[str, Any]:
    """
    Ping the worker and return its liveness response.
    """
    raise NotImplementedError("backend 'arq' does not support backend_ping_worker")


@tai42_app.tools.tool(tags={"backend"})
async def backend_list_active_workers() -> list[str]:
    """
    List the workers currently active.
    """
    raise NotImplementedError("backend 'arq' does not support backend_list_active_workers")
