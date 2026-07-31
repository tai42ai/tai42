"""The uniform ``backend_*`` tool surface over the RQ broker.

Task/worker tools read RQ's raw Redis structures (worker hashes, queue lists, the
result stream); schedule tools drive the RQ scheduler. The host's schedule marker
tools are ``backend_list_schedules`` / ``backend_delete_schedule`` /
``backend_export_schedules`` / ``backend_import_schedules``. The scheduler API and
``Job`` accessors are blocking, so scheduler-touching tools use the sync client in
a thread; the raw-structure tools use the async client directly.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import pickle
import time
import zlib
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from rq.exceptions import NoSuchJobError
from rq.job import JobStatus
from rq_scheduler import Scheduler
from tai42_contract.app import tai42_app
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient, SyncRedisClient
from tai42_kit.utils.runtime.schedule_util import normalize_schedule

from tai42_backend_rq.liveness import heartbeat_fresh
from tai42_backend_rq.schedules import ScheduleRecord, apply_normalized_schedule, crontab_string, interval_seconds
from tai42_backend_rq.settings import RqSettings, rq_settings
from tai42_backend_rq.tasks import tool_execution


def _async_redis(settings: RqSettings) -> AbstractAsyncContextManager[Any]:
    """The pooled async Redis client, typed ``Any``: redis-py shares one command
    stub between its sync and async clients, so the erasure keeps every ``await
    r...`` call sound."""
    return client_ctx(RedisClient, url=settings.redis_url)


def _sync_redis(settings: RqSettings) -> AbstractAsyncContextManager[Any]:
    """The pooled sync Redis client (for the blocking scheduler/Job APIs),
    typed ``Any`` for the same stub-sharing reason as :func:`_async_redis`."""
    return client_ctx(SyncRedisClient, url=settings.redis_url)


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _heartbeat_fresh(raw: bytes | str) -> bool:
    """Whether a worker's stored ``last_heartbeat`` proves it is alive. Parses the
    stored value; the freshness window is the shared contract in
    :mod:`tai42_backend_rq.liveness`."""
    return heartbeat_fresh(datetime.fromisoformat(_text(raw).replace("Z", "+00:00")))


def _next_run_fields(ts: float | None) -> dict[str, Any]:
    """The pair of next-run fields the schedule tools report from a zset score."""
    if ts is None:
        return {"next_run_at_ts": None, "next_run_at_iso": None}
    return {"next_run_at_ts": ts, "next_run_at_iso": datetime.fromtimestamp(ts, tz=UTC).isoformat()}


async def _worker_names(r: Any, settings: RqSettings) -> list[str]:
    """Every registered worker's bare name (RQ's registry set stores full keys
    ``rq:worker:<name>``, so the prefix is stripped)."""
    members = await r.smembers(settings.rq_workers_key)
    prefix = settings.rq_worker_key("")
    return [_text(m).removeprefix(prefix) for m in members]


async def _queue_names(r: Any, settings: RqSettings) -> list[str]:
    """Every registered queue's bare name (RQ's registry set stores full keys
    ``rq:queue:<name>``, so the prefix is stripped)."""
    members = await r.smembers(settings.rq_queues_key)
    prefix = settings.rq_queue_key("")
    return [_text(m).removeprefix(prefix) for m in members]


@tai42_app.tools.tool(tags={"backend"})
async def backend_ping_worker(worker_name: str | None = None) -> dict[str, Any]:
    """Ping all workers or a specific worker to check if they're alive (based on recent heartbeat)."""
    settings = rq_settings()
    async with _async_redis(settings) as r:
        ping: dict[str, Any] = {}
        workers = [worker_name] if worker_name else await _worker_names(r, settings)
        for w in workers:
            hb = await r.hget(settings.rq_worker_key(w), "last_heartbeat")
            if hb and _heartbeat_fresh(hb):
                ping[w] = "pong"
        return ping


@tai42_app.tools.tool(tags={"backend"})
async def backend_list_active_workers() -> list[str]:
    """Get a list of all currently connected and responsive worker names."""
    ping = await backend_ping_worker(None)
    return list(ping.keys())


@tai42_app.tools.tool(tags={"backend"})
async def backend_task_status(task_id: str) -> str:
    """Return the current status of a given task ID."""
    settings = rq_settings()
    async with _async_redis(settings) as r:
        status_raw = await r.hget(settings.rq_job_key(task_id), "status")
        if status_raw is None:
            return "unknown"
        return _text(status_raw)


async def _read_failure_reason(r: Any, settings: RqSettings, task_id: str) -> str:
    """The failed task's stored exception traceback text (its newest result). RQ
    persists a failed job's exception only as compressed ``exc_string`` in the
    result stream; when absent, no details are retained."""
    entries = await r.xrevrange(settings.rq_result_key(task_id), count=1)
    if entries:
        raw = entries[0][1].get(b"exc_string")
        if raw:
            return zlib.decompress(base64.b64decode(raw)).decode()
    return "no failure details retained"


@tai42_app.tools.tool(tags={"backend"})
async def backend_task_result(task_id: str, timeout: float | None = None) -> Any:
    """Return the result of a completed task by ID.

    When ``timeout`` is given, poll until the task finishes or the timeout (in
    seconds) elapses before reading the result. When ``timeout`` is None, read
    the current state without waiting. A FAILED task raises the task's stored
    failure (RQ retains a failed job's exception as its traceback text, so
    that text is what the raised error carries).
    """
    settings = rq_settings()
    async with _async_redis(settings) as r:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            status_raw = await r.hget(settings.rq_job_key(task_id), "status")
            if status_raw is None:
                return f"Task {task_id} not found"
            status = _text(status_raw)
            if status == JobStatus.FINISHED:
                break
            if status == JobStatus.FAILED:
                raise RuntimeError(f"Task {task_id} failed: {await _read_failure_reason(r, settings, task_id)}")
            if status in (JobStatus.STOPPED, JobStatus.CANCELED):
                # Terminal without a result or stored failure: stop waiting.
                return f"Task {task_id} did not complete (status: {status})"
            if deadline is None or time.monotonic() >= deadline:
                return f"Task {task_id} is not ready (status: {status})"
            await asyncio.sleep(0.25)

        stream_key = settings.rq_result_key(task_id)
        # Newest-first: a recurring job reuses its id, so only the latest entry
        # is "the" result.
        stream_entries = await r.xrevrange(stream_key, count=1)
        if not stream_entries:
            return f"No result found for task {task_id} in stream {stream_key}"

        entry = stream_entries[0]
        result_bytes = entry[1].get(b"return_value")
        if result_bytes:
            try:
                return pickle.loads(base64.b64decode(result_bytes))
            except (binascii.Error, pickle.UnpicklingError) as e:
                raise ValueError(f"Failed to decode result for task {task_id}: {e}") from e
        return None


@tai42_app.tools.tool(tags={"backend"})
async def backend_cancel_task(task_id: str) -> str:
    """Cancel a running or queued task."""
    settings = rq_settings()
    async with _sync_redis(settings) as r:
        scheduler = Scheduler(connection=r)

        def _cancel() -> str:
            # A scheduled (recurring) entry cancels through the scheduler.
            if task_id in scheduler:
                scheduler.cancel(task_id)
                return f"Scheduled Task {task_id} canceled"

            job_key = settings.rq_job_key(task_id)
            if not r.exists(job_key):
                return f"Task {task_id} not found"
            r.delete(job_key)
            return f"Task {task_id} canceled"

        return await asyncio.to_thread(_cancel)


@tai42_app.tools.tool(tags={"backend"})
async def backend_active_tasks(worker_name: str | None = None) -> dict[str, Any]:
    """Get all currently executing tasks on the worker(s).

    Returns a mapping of worker name to a list with that worker's current job
    (``{"id", "data"}``; RQ runs one job per worker at a time).
    """
    settings = rq_settings()
    async with _async_redis(settings) as r:
        active: dict[str, Any] = {}
        workers = [worker_name] if worker_name else await _worker_names(r, settings)
        for w in workers:
            cj = await r.hget(settings.rq_worker_key(w), "current_job")
            if cj:
                job_id = _text(cj)
                job_data = await r.hgetall(settings.rq_job_key(job_id))
                active[w] = [
                    {
                        "id": job_id,
                        "data": {k.decode(): v.decode() for k, v in job_data.items() if isinstance(k, bytes)},
                    }
                ]
        return active


@tai42_app.tools.tool(tags={"backend"})
async def backend_reserved_tasks() -> dict[str, Any]:
    """Get all reserved tasks (queued but not yet started).

    Returns a mapping of queue name to that queue's list of job ids — RQ
    reserves work per queue, not per worker, so the map is keyed by queue.
    """
    settings = rq_settings()
    async with _async_redis(settings) as r:
        reserved: dict[str, Any] = {}
        for q in await _queue_names(r, settings):
            job_ids = await r.lrange(settings.rq_queue_key(q), 0, -1)
            reserved[q] = [_text(jid) for jid in job_ids]
        return reserved


@tai42_app.tools.tool(tags={"backend"})
async def backend_scheduled_tasks() -> dict[str, Any]:
    """Get all scheduled tasks (waiting for ETA/countdown or a recurrence).

    Returns a mapping of job id to ``{"next_run_at_ts": <epoch seconds>}``.
    RQ keeps the two kinds in different zsets — recurring schedules in the RQ
    scheduler's zset, ETA/countdown jobs in each queue's scheduled-job
    registry — so both are read (each zset scores in epoch seconds).
    """
    settings = rq_settings()
    async with _async_redis(settings) as r:
        scheduled: dict[str, Any] = {}
        items = await r.zrange(settings.rq_scheduler_zset, 0, -1, withscores=True)
        for member, score in items:
            scheduled[_text(member)] = {"next_run_at_ts": float(score)}
        for q in await _queue_names(r, settings):
            items = await r.zrange(settings.rq_scheduled_registry_key(q), 0, -1, withscores=True)
            for member, score in items:
                scheduled[_text(member)] = {"next_run_at_ts": float(score)}
        return scheduled


@tai42_app.tools.tool(tags={"backend"})
async def backend_registered_tasks() -> dict[str, Any]:
    """List task types registered with the backend."""
    raise NotImplementedError("backend 'rq' does not support backend_registered_tasks")


@tai42_app.tools.tool(tags={"backend"})
async def backend_list_failed_tasks() -> list[dict[str, Any]]:
    """List tasks that have failed."""
    raise NotImplementedError("backend 'rq' does not support backend_list_failed_tasks")


@tai42_app.tools.tool(tags={"backend"})
async def backend_worker_stats(worker_name: str | None = None) -> dict[str, Any]:
    """Get worker statistics and info.

    Returns a mapping of worker name to that worker's registry hash (state,
    heartbeat, queues, counters) decoded to text.
    """
    settings = rq_settings()
    async with _async_redis(settings) as r:
        stats: dict[str, Any] = {}
        workers = [worker_name] if worker_name else await _worker_names(r, settings)
        for w in workers:
            worker_data = await r.hgetall(settings.rq_worker_key(w))
            stats[w] = {k.decode(): v.decode() for k, v in worker_data.items() if isinstance(k, bytes)}
        return stats


@tai42_app.tools.tool(tags={"backend"})
async def backend_worker_queues(worker_name: str | None = None) -> dict[str, Any]:
    """List all queues each worker is consuming from."""
    settings = rq_settings()
    async with _async_redis(settings) as r:
        queues_dict: dict[str, Any] = {}
        workers = [worker_name] if worker_name else await _worker_names(r, settings)
        for w in workers:
            queues = await r.hget(settings.rq_worker_key(w), "queues")
            if queues:
                queues_str = _text(queues)
                queues_list = json.loads(queues_str) if queues_str.startswith("[") else queues_str.split(",")
                queues_dict[w] = queues_list
        return queues_dict


@tai42_app.tools.tool(tags={"backend"})
async def backend_delete_schedule(name: str) -> dict[str, Any]:
    """Remove a periodic task from the scheduler."""
    settings = rq_settings()
    async with _async_redis(settings) as r:
        removed = await r.zrem(settings.rq_scheduler_zset, name)
        if removed:
            await r.delete(settings.rq_job_key(name))
            return {"status": "deleted", "name": name}
        return {"status": "not_found", "name": name}


@tai42_app.tools.tool(tags={"backend"})
async def backend_list_schedules() -> list[dict[str, Any]]:
    """List all schedule entries.

    Each row carries the canonical keys ``name``, ``enabled``,
    ``next_run_at_ts`` and ``next_run_at_iso``, plus the backend-specific
    ``meta`` extra (the job meta holding the interval seconds or cron string).
    ``enabled`` is always true — RQ has no disabled-schedule state (disabling
    deletes the job), so every listed schedule is live.
    """
    settings = rq_settings()
    async with _sync_redis(settings) as r:
        scheduler = Scheduler(connection=r)

        def _read() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            # with_times pairs each job with its next run (a naive-UTC datetime).
            pairs = cast("list[tuple[Any, datetime]]", scheduler.get_jobs(with_times=True))
            for job, next_run in pairs:
                run_at = next_run.replace(tzinfo=UTC) if next_run.tzinfo is None else next_run
                out.append(
                    {
                        "name": job.id,
                        "enabled": True,
                        **_next_run_fields(run_at.timestamp()),
                        "meta": dict(job.meta or {}),
                    }
                )
            return out

        return await asyncio.to_thread(_read)


@tai42_app.tools.tool(tags={"backend"})
async def backend_get_schedule(name: str) -> dict[str, Any]:
    """Get schedule details."""
    settings = rq_settings()
    async with _sync_redis(settings) as r:
        scheduler = Scheduler(connection=r)

        def _read() -> dict[str, Any]:
            if name not in scheduler:
                return {"status": "not_found", "name": name}
            try:
                job = scheduler.job_class.fetch(name, connection=r)
            except NoSuchJobError:
                return {"status": "not_found", "name": name}

            meta = dict(job.meta or {})
            schedule_def: dict[str, Any] = {}
            if meta.get("interval"):
                schedule_def = {"__type__": "interval", "every": meta["interval"]}
            elif meta.get("cron_string"):
                schedule_def = {"__type__": "crontab", "cron_string": meta["cron_string"]}

            score = r.zscore(settings.rq_scheduler_zset, name)
            return {
                "name": name,
                "definition": {"schedule": schedule_def},
                "meta": meta,
                **_next_run_fields(float(score) if score is not None else None),
            }

        return await asyncio.to_thread(_read)


@tai42_app.tools.tool(tags={"backend"})
async def backend_enable_schedule(name: str) -> dict[str, Any]:
    """Enable a schedule.

    RQ has no disabled-schedule state, so the closest honest operation is
    running the schedule now: this delegates to ``backend_run_schedule_now``
    and returns that op's own statuses (``queued`` / ``not_found``) — never an
    invented ``enabled``.
    """
    return await backend_run_schedule_now(name)


@tai42_app.tools.tool(tags={"backend"})
async def backend_disable_schedule(name: str) -> dict[str, Any]:
    """Disable a schedule.

    RQ has no disabled-schedule state — disabling IS deleting: this delegates
    to ``backend_delete_schedule`` and returns that op's own statuses
    (``deleted`` / ``not_found``) — never an invented ``disabled``.
    """
    return await backend_delete_schedule(name)


@tai42_app.tools.tool(tags={"backend"})
async def backend_run_schedule_now(name: str) -> dict[str, Any]:
    """Force a schedule to run ASAP.

    ``Scheduler.enqueue_job`` moves the scheduled job onto its queue for
    immediate execution and re-arms the recurrence itself (the next interval
    tick or the next cron time), so the schedule keeps running afterwards.
    """
    settings = rq_settings()
    async with _sync_redis(settings) as r:
        scheduler = Scheduler(connection=r)

        def _run() -> dict[str, Any]:
            if name not in scheduler:
                return {"status": "not_found", "name": name}
            job = scheduler.job_class.fetch(name, connection=r)
            scheduler.enqueue_job(job)
            return {"status": "queued", "name": name}

        return await asyncio.to_thread(_run)


@tai42_app.tools.tool(tags={"backend"})
async def backend_schedule_exists(name: str) -> bool:
    """Return True if a scheduled job with the given name exists."""
    settings = rq_settings()
    async with _async_redis(settings) as r:
        return await r.zscore(settings.rq_scheduler_zset, name) is not None


@tai42_app.tools.tool(tags={"backend"})
async def backend_update_schedule(
    name: str,
    new_schedule: int | float | str | dict[str, Any] | None = None,
    next_run_in_ms: int | None = None,
    next_run_at_ts: float | None = None,
) -> dict[str, Any]:
    """Update an existing schedule entry.

    A recurrence change (``new_schedule``) cancels the old job and re-creates
    it under the same name with the stored args/kwargs; the replaced kind's
    meta key is dropped so the schedule reads back as exactly one kind. A
    next-run change (``next_run_at_ts`` / ``next_run_in_ms``) moves the
    schedule's next execution time in place — for a crontab schedule it shifts
    only the next firing (the recurrence then resumes from the cron string).
    The reported next run is read back from the scheduler, not assumed.
    """
    settings = rq_settings()
    async with _sync_redis(settings) as r:
        scheduler = Scheduler(connection=r)

        def _update() -> dict[str, Any]:
            if name not in scheduler:
                return {"status": "not_found", "name": name}

            explicit_next_run: datetime | None = None
            if next_run_at_ts is not None:
                explicit_next_run = datetime.fromtimestamp(next_run_at_ts, tz=UTC)
            elif next_run_in_ms is not None:
                explicit_next_run = datetime.now(UTC) + timedelta(milliseconds=next_run_in_ms)

            if new_schedule is None and explicit_next_run is None:
                return {"status": "skipped", "message": "No changes provided"}

            old_job = scheduler.job_class.fetch(name, connection=r)
            args = old_job.args
            kwargs = old_job.kwargs
            old_meta = dict(old_job.meta or {})
            cron = old_meta.get("cron_string")
            interval = old_meta.get("interval")

            interval_recreated = False
            if new_schedule is not None:
                norm = normalize_schedule(new_schedule)
                # Checked before the cancel, so an unrepresentable interval raises
                # while the prior schedule is still intact.
                new_interval = interval_seconds(norm) if norm["__type__"] == "interval" else None
                # Keep every custom meta key but no stale recurrence key of the
                # replaced kind, so it reads back as one kind.
                passthrough_meta = {k: v for k, v in old_meta.items() if k not in ("interval", "cron_string")}

                # Recurrence lives on the stored job, so cancel and re-create it
                # under the same id.
                scheduler.cancel(name)
                if new_interval is not None:
                    interval = new_interval
                    cron = None
                    scheduler.schedule(
                        scheduled_time=explicit_next_run or datetime.now(UTC),
                        func=tool_execution,
                        args=args,
                        kwargs=kwargs,
                        interval=interval,
                        id=name,
                        meta={**passthrough_meta, "interval": interval},
                    )
                    interval_recreated = True
                else:  # crontab — normalize_schedule yields no other kind
                    cron = crontab_string(norm)
                    interval = None
                    scheduler.cron(
                        cron,
                        func=tool_execution,
                        args=args,
                        kwargs=kwargs,
                        id=name,
                        meta={**passthrough_meta, "cron_string": cron},
                    )

            if explicit_next_run is not None and not interval_recreated:
                # In-place next-run move; raises loudly if the schedule vanished.
                scheduler.change_execution_time(old_job, explicit_next_run)

            score = r.zscore(settings.rq_scheduler_zset, name)
            return {
                "status": "updated",
                "name": name,
                "new_schedule": {"interval": interval, "cron": cron},
                **_next_run_fields(float(score) if score is not None else None),
            }

        return await asyncio.to_thread(_update)


@tai42_app.tools.tool(tags={"backend"})
async def backend_export_schedules() -> list[dict[str, Any]]:
    """Export every schedule as a portable record that round-trips through backend_import_schedules.

    Reads each scheduled Job directly, taking its args and kwargs (the tool
    name the job runs) and reconstructing the canonical schedule dict from the
    Job ``meta`` (interval seconds or cron string). RQ has no persisted
    "enabled" flag because disabling a schedule deletes its job, so every
    exported record is emitted with ``enabled`` set to True. Each record's name
    is the Job id.
    """
    settings = rq_settings()
    async with _sync_redis(settings) as r:
        scheduler = Scheduler(connection=r)

        def _read_jobs() -> list[dict[str, Any]]:
            # Each Job field triggers a blocking Redis fetch; materialize them all
            # here so the whole read stays off the event loop.
            read: list[dict[str, Any]] = []
            for job in cast("list[Any]", scheduler.get_jobs()):
                read.append(
                    {
                        "id": job.id,
                        "args": list(job.args),
                        "kwargs": dict(job.kwargs),
                        "meta": dict(job.meta or {}),
                    }
                )
            return read

        jobs = await asyncio.to_thread(_read_jobs)

        records = []
        for job in jobs:
            meta = job["meta"]
            if "interval" in meta:
                schedule = normalize_schedule(meta["interval"])
            elif "cron_string" in meta:
                schedule = normalize_schedule(meta["cron_string"])
            else:
                raise ValueError(
                    f"Schedule {job['id']!r} has no 'interval' or 'cron_string' in its job meta; "
                    "cannot reconstruct its schedule"
                )

            record = ScheduleRecord(
                name=job["id"],
                args=job["args"],
                kwargs=job["kwargs"],
                schedule=schedule,
                enabled=True,
            )
            records.append(record.model_dump())
        return records


@tai42_app.tools.tool(tags={"backend"})
async def backend_import_schedules(schedules: list[dict[str, Any]]) -> dict[str, Any]:
    """Import schedule records produced by backend_export_schedules, upserting by name.

    Each entry is validated as a ScheduleRecord and its schedule dict is
    re-normalized so a partial or non-canonical schedule is validated before
    any write. The canonical schedule is applied through the same logic the
    schedule-create path uses: an interval schedule with ``scheduler.schedule``
    and a crontab schedule with ``scheduler.cron``, restoring the stored args
    and kwargs under the record's name as the Job id. The create is keyed on
    that id, so it overwrites an existing job in place; no pre-cancel is
    performed, so a failure mid-apply never destroys the prior schedule.
    Whether a name already existed is read (read-only) before the write to
    classify the row as "updated" versus "created".

    The RQ backend has no disabled-schedule representation — disabling a
    schedule deletes its job — so a record with ``enabled=False`` is NOT
    enqueued as a live job; it is skipped and the reason is surfaced in
    "errors". A canonical interval carrying ``relative=True`` cannot be
    represented either, so the schedule is still created but the dropped flag
    is surfaced in "errors". A record that fails validation or application is
    collected in "errors" with its reason and never dropped silently.

    Returns ``{"created": int, "updated": int, "skipped": int, "errors": [...]}``
    to match the shared section-report contract; each error row is
    ``{"index", "name", "error"}``.
    """
    settings = rq_settings()
    created = 0
    updated = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    async with _sync_redis(settings) as r:
        scheduler = Scheduler(connection=r)
        for index, entry in enumerate(schedules):
            entry_name = entry.get("name") if isinstance(entry, dict) else None
            try:
                record = ScheduleRecord.model_validate(entry)
                name = record.name

                # Re-normalize before any write so a partial schedule is validated.
                norm = normalize_schedule(record.schedule)

                # RQ cannot store a disabled schedule (disable == delete), so
                # importing one as a live job would silently reactivate it.
                if not record.enabled:
                    skipped += 1
                    errors.append(
                        {
                            "index": index,
                            "name": name,
                            "error": (
                                f"schedule {name!r} has enabled=False; the RQ backend has no "
                                "disabled-schedule representation (disabling deletes the job), so "
                                "it was skipped rather than created as an active schedule"
                            ),
                        }
                    )
                    continue

                # Read-only existence check before the write, to classify
                # created vs updated.
                exists = await asyncio.to_thread(scheduler.__contains__, name)

                await apply_normalized_schedule(scheduler, norm, tool_execution, record.args, record.kwargs, name)

                # A relative=True interval cannot be represented; the schedule was
                # created, so surface the dropped flag (only after a successful apply).
                if norm["__type__"] == "interval" and norm.get("relative"):
                    errors.append(
                        {
                            "index": index,
                            "name": name,
                            "error": (
                                f"schedule {name!r} has relative=True, which the RQ backend cannot "
                                "represent; the schedule was created with the relative flag ignored"
                            ),
                        }
                    )

                if exists:
                    updated += 1
                else:
                    created += 1
            except Exception as exc:
                # Collect-and-continue: one bad record must not abort the batch.
                errors.append({"index": index, "name": entry_name, "error": repr(exc)})
        return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}
