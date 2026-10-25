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
from arq.jobs import JobStatus, ResultNotFound
from arq.utils import timestamp_ms
from tai42_contract.app import tai42_app
from tai42_contract.backend import (
    CrontabCadence,
    IntervalCadence,
    ScheduleImportError,
    ScheduleImportMode,
    ScheduleImportReport,
    ScheduleListRow,
    ScheduleRecord,
    ScheduleRowError,
)
from tai42_kit.utils.runtime.schedule_util import normalize_schedule

from tai42_backend_arq import pool
from tai42_backend_arq.jobs import job_handle, known_queues
from tai42_backend_arq.records import (
    derive_cron_or_interval,
    next_run_after,
    parse_cron_or_interval,
    read_stored_cadence,
    refuse_relative,
)
from tai42_backend_arq.scheduler import (
    abort_job,
    abort_schedule_task,
    safe_schedule_transition,
    schedule_lock,
    wait_job_result,
)
from tai42_backend_arq.settings import TaskFailedError, arq_settings

logger = logging.getLogger(__name__)


def _decode_optional(value: bytes | None) -> str | None:
    """Decode an optional schedule-hash field, ``None`` when the field is absent."""
    return value.decode() if value else None


def _stored_cadence(name: str, data: dict[bytes, bytes]) -> IntervalCadence | CrontabCadence:
    """The cadence a schedule hash stores; an un-reset hash raises ``ScheduleStoreFormatError``."""
    return read_stored_cadence(name, data.get(b"schedule"), arq_settings().arq_schedule_pattern)


def _split_tool(name: str, kwargs: dict[str, Any]) -> str:
    """Pop and return the fired tool's name from a schedule's stored ``kwargs``; a hash without it raises."""
    tool_name_arg = arq_settings().tool_name_arg
    if tool_name_arg not in kwargs:
        raise ValueError(
            f"schedule {name!r} carries no {tool_name_arg!r}; it was not created through this backend's schedule door"
        )
    return kwargs.pop(tool_name_arg)


def _row_error(data: dict[bytes, bytes]) -> ScheduleRowError | None:
    """The startup watchdog's terminal recovery failure recorded on a schedule hash, if any."""
    message = _decode_optional(data.get(b"recovery_error"))
    if message is None:
        return None
    return ScheduleRowError(message=message, at_iso=_decode_optional(data.get(b"recovery_failed_at")))


def _failure_detail(result: Any) -> str:
    """Human-readable detail of a stored failure result.

    The revived failure's ``repr``, or the ``repr`` of whatever else was stored.
    """
    if isinstance(result, TaskFailedError):
        return result.error_repr
    if isinstance(result, asyncio.CancelledError):
        return str(result) or repr(result)
    return repr(result)


@tai42_app.tools.tool(tags={"backend"})
async def backend_task_status(task_id: str) -> str:
    """Return the current status of a given task ID."""
    async with pool.arq_connection() as arq_redis:
        job = await job_handle(arq_redis, task_id)
        status = await job.status()
        return status.value if status else "unknown"


@tai42_app.tools.tool(tags={"backend"})
async def backend_task_result(task_id: str, timeout: float | None = None) -> Any:
    """Return the result of a completed task by ID.

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
    async with pool.arq_connection() as arq_redis:
        job = await job_handle(arq_redis, task_id)
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
    """Cancel (abort) a running or queued task.

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
    async with pool.arq_connection() as arq_redis:
        job = await job_handle(arq_redis, task_id)
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
    """Get all currently executing tasks (in-progress jobs).

    Returns a flat mapping of job id to ``{"status": "in_progress"}`` — arq has
    no per-worker attribution for a running job, so the map is keyed by job id,
    not by worker.
    """
    async with pool.arq_connection() as arq_redis:
        # arq marks a running job with an in-progress key; the job's status API is
        # the authority (a lingering key after completion reads complete).
        pattern = f"{in_progress_key_prefix}*".encode()
        job_ids: list[str] = []
        async for key in arq_redis.scan_iter(match=pattern):
            raw = key.decode() if isinstance(key, bytes) else key
            job_ids.append(raw[len(in_progress_key_prefix) :])
        tasks: dict[str, Any] = {}
        for job_id in job_ids:
            status = await (await job_handle(arq_redis, job_id)).status()
            if status == JobStatus.in_progress:
                tasks[job_id] = {"status": status.value}
        return tasks


@tai42_app.tools.tool(tags={"backend"})
async def backend_reserved_tasks() -> dict[str, list[str]]:
    """Get all reserved/queued tasks (due to run, not yet picked up).

    Returns due job ids keyed by queue: the settings queue and every derived queue a
    task was sent to, an empty queue as ``[]``.
    """
    async with pool.arq_connection() as arq_redis:
        now_ms = timestamp_ms()
        reserved: dict[str, list[str]] = {}
        for queue in await known_queues(arq_redis):
            reserved[queue] = [
                job.job_id
                for job in await arq_redis.queued_jobs(queue_name=queue)
                if job.job_id is not None and job.score is not None and job.score <= now_ms
            ]
        return reserved


@tai42_app.tools.tool(tags={"backend"})
async def backend_scheduled_tasks() -> dict[str, float]:
    """Get all scheduled/deferred tasks (future-dated queue entries).

    Returns a flat mapping of job id to its due time in milliseconds since the
    epoch (the queue zset score), across the settings queue and every derived queue.
    """
    async with pool.arq_connection() as arq_redis:
        now_ms = timestamp_ms()
        scheduled: dict[str, float] = {}
        for queue in await known_queues(arq_redis):
            scheduled.update(
                {
                    job.job_id: float(job.score)
                    for job in await arq_redis.queued_jobs(queue_name=queue)
                    if job.job_id is not None and job.score is not None and job.score > now_ms
                }
            )
        return scheduled


@tai42_app.tools.tool(tags={"backend"})
async def backend_list_failed_tasks() -> list[dict[str, Any]]:
    """Get the failed (including aborted) tasks whose results are still retained.

    Returns ``{"task_id", "error"}`` rows — ``error`` carries the stored failure detail (the
    original exception's ``repr``; an aborted task's detail names its ``CancelledError``). arq
    keeps job outcomes only for the configured keep-result window, so this lists failures within
    that window.
    """
    async with pool.arq_connection() as arq_redis:
        return [
            {"task_id": result.job_id, "error": _failure_detail(result.result)}
            for result in await arq_redis.all_job_results()
            if not result.success and result.job_id is not None
        ]


@tai42_app.tools.tool(tags={"backend"})
async def backend_list_schedules() -> list[dict[str, Any]]:
    """List all custom schedules as ``ScheduleListRow`` dumps.

    Each row carries ``name``, the fired ``tool``, the ``cadence``, ``enabled``,
    ``next_run_at_ts`` and ``next_run_at_iso`` (the pending job's due time, read from the
    schedule hash; null when none is recorded yet) — never the stored arguments.
    ``error`` carries the terminal recovery failure the startup watchdog records on a
    schedule it cannot recover (null on a healthy schedule). A hash in a format this
    backend no longer reads raises ``ScheduleStoreFormatError``.
    """
    async with pool.arq_connection() as arq_redis:
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
                ScheduleListRow(
                    name=name,
                    tool=_split_tool(name, orjson.loads(data.get(b"kwargs", b"{}"))),
                    cadence=_stored_cadence(name, data),
                    enabled=data.get(b"enabled", b"true").decode() == "true",
                    next_run_at_ts=next_run_ts,
                    next_run_at_iso=(datetime.fromtimestamp(next_run_ts, tz=UTC).isoformat() if next_run_ts else None),
                    error=_row_error(data),
                ).model_dump(mode="json")
            )
        return schedules


@tai42_app.tools.tool(tags={"backend"})
async def backend_export_schedules() -> list[dict[str, Any]]:
    """Export every custom schedule as a list of portable, JSON-serializable records.

    Each ``ScheduleRecord`` captures only the durable definition of a schedule -- its
    name, the fired ``tool`` (split out of the stored kwargs' tool-name key), positional
    args, the remaining keyword args, the cadence, and whether it is enabled.
    Runtime-only fields (job_id, last_scheduled_ts, aborted, cron_or_interval)
    are omitted because they are derivable from the schedule at import time. The
    returned list round-trips through ``backend_import_schedules``. A schedule
    hash with a missing or unreadable stored cadence raises ``ScheduleStoreFormatError``
    — an export must never emit a record that cannot recreate its schedule.
    """
    async with pool.arq_connection() as arq_redis:
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
            kwargs = orjson.loads(data.get(b"kwargs", b"{}"))
            record = ScheduleRecord(
                name=name,
                tool=_split_tool(name, kwargs),
                args=orjson.loads(data.get(b"args", b"[]")),
                kwargs=kwargs,
                cadence=_stored_cadence(name, data),
                enabled=data.get(b"enabled", b"true").decode() == "true",
            )
            records.append(record)
        return [record.model_dump(mode="json") for record in records]


@tai42_app.tools.tool(tags={"backend"})
async def backend_import_schedules(
    schedules: list[dict[str, Any]], mode: ScheduleImportMode = "skip"
) -> dict[str, Any]:
    """Import schedules previously produced by ``backend_export_schedules``.

    Each entry is parsed as a ``ScheduleRecord`` (a relative interval is refused as a
    per-row error) and written through the same low-level schedule-hash mapping the
    create path uses: target is set to ``tool_execution`` and the args, the kwargs (the
    record's ``tool`` injected back under the tool-name key), the cadence dump, derived
    cron_or_interval and enabled flag are stored under ``arq:schedule:{name}``.
    The next run is recomputed from the schedule (never taken from a stale
    exported value).

    ``mode`` keys an existing name: under ``skip`` (the default) an existing name is
    left untouched -- not aborted, not re-applied -- and counted in
    ``skipped_existing``; under ``overwrite`` the existing schedule is replaced in
    place (counted ``updated``). A new name is created (counted ``created``) under
    both. This is the per-record import mode the backup restore forwards across the
    tool boundary, honored here as every backend honors it.

    A malformed entry or a failed write is recorded in ``errors`` -- never silently
    dropped. Returns a ``ScheduleImportReport`` dump; each error row is
    ``{"index", "name", "error"}``. ``skipped`` is
    always 0 -- this backend has no per-row skip case (every valid record can be
    stored, enabled or disabled).
    """
    async with pool.arq_connection() as arq_redis:
        settings = arq_settings()

        created = 0
        updated = 0
        skipped = 0
        skipped_existing = 0
        errors: list[ScheduleImportError] = []

        for index, entry in enumerate(schedules):
            try:
                record = ScheduleRecord.model_validate(entry)
                refuse_relative(record.cadence)

                key = settings.arq_schedule_key(record.name)
                exists = bool(await arq_redis.exists(key))

                if exists and mode == "skip":
                    # An existing schedule (keyed by name) is left in place.
                    skipped_existing += 1
                    continue

                cron_or_interval = derive_cron_or_interval(record.cadence)
                defer_by, last_scheduled_ts = next_run_after(cron_or_interval, datetime.now(UTC))

                mapping_updates = {
                    "target": "tool_execution",
                    "args": orjson.dumps(record.args),
                    "kwargs": orjson.dumps({**record.kwargs, settings.tool_name_arg: record.tool}),
                    "schedule": record.cadence.model_dump_json(),
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
                raw_name = entry.get("name") if isinstance(entry, dict) else None
                name = raw_name if isinstance(raw_name, str) else None
                errors.append(ScheduleImportError(index=index, name=name, error=repr(exc)))

        return ScheduleImportReport(
            created=created, updated=updated, skipped=skipped, skipped_existing=skipped_existing, errors=errors
        ).model_dump()


@tai42_app.tools.tool(tags={"backend"})
async def backend_get_schedule(name: str) -> dict[str, Any]:
    """Get details of a custom schedule (its stored cadence validated; an un-reset hash raises)."""
    async with pool.arq_connection() as arq_redis:
        settings = arq_settings()
        key = settings.arq_schedule_key(name)
        data = await arq_redis.hgetall(key)
        if not data:
            # Empty hash means no schedule.
            return {"status": "not_found", "name": name}
        return {
            "name": name,
            "enabled": data.get(b"enabled", b"true").decode() == "true",
            "schedule": _stored_cadence(name, data).model_dump(mode="json"),
            "target": data.get(b"target", b"").decode(),
            "args": orjson.loads(data.get(b"args", b"[]")),
            "kwargs": orjson.loads(data.get(b"kwargs", b"{}")),
            "recovery_error": _decode_optional(data.get(b"recovery_error")),
            "recovery_failed_at": _decode_optional(data.get(b"recovery_failed_at")),
        }


@tai42_app.tools.tool(tags={"backend"})
async def backend_delete_schedule(name: str) -> dict[str, Any]:
    """Delete a custom schedule.

    Runs under the per-schedule lock so the delete serializes with schedule
    transitions and flag writes — a transition mid-flight can never re-write
    the hash after this delete removed it.
    """
    async with pool.arq_connection() as arq_redis:
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
    """Enable a custom schedule.

    The flag write runs under the per-schedule lock, after an existence check
    that stays true for the write — writing the flag into a hash a concurrent
    delete just removed would resurrect the schedule as a partial hash.
    """
    async with pool.arq_connection() as arq_redis:
        settings = arq_settings()
        key = settings.arq_schedule_key(name)
        async with schedule_lock(arq_redis, name):
            if not await arq_redis.exists(key):
                return {"status": "not_found"}
            await arq_redis.hset(key, "enabled", "true")
        return {"status": "enabled"}


@tai42_app.tools.tool(tags={"backend"})
async def backend_disable_schedule(name: str) -> dict[str, Any]:
    """Disable a custom schedule.

    The flag write runs under the per-schedule lock, after an existence check
    that stays true for the write — writing the flag into a hash a concurrent
    delete just removed would resurrect the schedule as a partial hash.
    """
    async with pool.arq_connection() as arq_redis:
        settings = arq_settings()
        key = settings.arq_schedule_key(name)
        async with schedule_lock(arq_redis, name):
            if not await arq_redis.exists(key):
                return {"status": "not_found"}
            await arq_redis.hset(key, "enabled", "false")
        return {"status": "disabled"}


@tai42_app.tools.tool(tags={"backend"})
async def backend_run_schedule_now(name: str) -> dict[str, Any]:
    """Force a schedule to run ASAP."""
    async with pool.arq_connection() as arq_redis:
        settings = arq_settings()
        key = settings.arq_schedule_key(name)
        data = await arq_redis.hgetall(key)
        if not data:
            # Empty hash means no schedule.
            return {"status": "not_found"}

        target = data.get(b"target", b"").decode()

        args = orjson.loads(data.get(b"args", b"[]"))
        kwargs = orjson.loads(data.get(b"kwargs", b"{}"))

        await arq_redis.enqueue_job(target, *args, _queue_name=settings.queue_name, **kwargs)
        return {"status": "queued"}


@tai42_app.tools.tool(tags={"backend"})
async def backend_schedule_exists(name: str) -> bool:
    """Return True if a custom schedule entry exists."""
    async with pool.arq_connection() as arq_redis:
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
    """Update an existing custom schedule entry.

    - `new_schedule` (optional): int/float (seconds interval), str (5-field crontab), or a
      friendly/``kind``-tagged dict, parsed into a cadence (a relative interval is refused).
      If omitted, the schedule is left unchanged.
    - `next_run_in_ms` (optional): delay in milliseconds from *now* (UTC). For initial enqueue.
    - `next_run_at_ts` (optional): absolute UNIX timestamp (UTC). Ignored if `next_run_in_ms` is provided.
    - If neither `new_schedule` nor `next_run_*` is given → returns {"status": "skipped"}.

    Returns: dict with status, previous_schedule, new_schedule, next_run_at_ts/iso, redis_key.
    """
    async with pool.arq_connection() as arq_redis:
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
        prev_schedule = _stored_cadence(name, data).model_dump(mode="json")
        prev_cron_or_interval = parse_cron_or_interval(data.get(b"cron_or_interval", b"").decode("utf-8"))

        updated = False
        cron_or_interval = prev_cron_or_interval
        mapping_updates: dict[str, Any] = {}

        if new_schedule is not None:
            cadence = normalize_schedule(new_schedule)
            refuse_relative(cadence)
            cron_or_interval = derive_cron_or_interval(cadence)
            mapping_updates["schedule"] = cadence.model_dump_json()
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
        stored_schedule = _stored_cadence(name, new_data).model_dump(mode="json")

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
    """List the task functions registered with the worker."""
    raise NotImplementedError("backend 'arq' does not support backend_registered_tasks")


@tai42_app.tools.tool(tags={"backend"})
async def backend_worker_stats() -> dict[str, Any]:
    """Return runtime statistics for the worker."""
    raise NotImplementedError("backend 'arq' does not support backend_worker_stats")


@tai42_app.tools.tool(tags={"backend"})
async def backend_worker_queues() -> list[str]:
    """List the queues the worker consumes from."""
    raise NotImplementedError("backend 'arq' does not support backend_worker_queues")


@tai42_app.tools.tool(tags={"backend"})
async def backend_ping_worker() -> dict[str, Any]:
    """Ping the worker and return its liveness response."""
    raise NotImplementedError("backend 'arq' does not support backend_ping_worker")


@tai42_app.tools.tool(tags={"backend"})
async def backend_list_active_workers() -> list[str]:
    """List the workers currently active."""
    raise NotImplementedError("backend 'arq' does not support backend_list_active_workers")
