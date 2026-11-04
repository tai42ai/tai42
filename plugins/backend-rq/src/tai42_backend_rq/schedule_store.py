"""The RQ schedule store: each schedule an rq-scheduler job firing ``tool_execution``.

A schedule is the scheduled job whose id is its name, plus its member in the scheduler's zset,
scored with the next run's timestamp. The job ``meta`` mirrors the cadence (the interval seconds
or the cron string), so a read rebuilds it. The scheduler holds no disabled job: every stored
schedule is enabled. The scheduler API and the ``Job`` accessors are blocking, so every call runs
on a thread over the pooled sync Redis client.
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, cast

from rq.exceptions import NoSuchJobError
from tai42_contract.backend import CrontabCadence, IntervalCadence, ScheduleRecord, StoredSchedule
from tai42_kit.backend import UnsupportedBackendTool, fired_kwargs
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import SyncRedisClient
from tai42_kit.utils.runtime.schedule_util import normalize_schedule

from tai42_backend_rq.backend import RqBackend
from tai42_backend_rq.schedules import apply_normalized_schedule, interval_seconds, scheduler_for
from tai42_backend_rq.settings import rq_client_kwargs, rq_settings
from tai42_backend_rq.tasks import tool_execution


def _sync_redis() -> AbstractAsyncContextManager[Any]:
    """The pooled sync Redis client for the blocking scheduler and ``Job`` APIs, typed ``Any``.

    redis-py shares one command stub between its sync and async clients, so the erasure keeps each
    call sound.
    """
    return client_ctx(SyncRedisClient, **rq_client_kwargs())


def _cadence_from_meta(name: str, meta: dict[str, Any]) -> IntervalCadence | CrontabCadence:
    """The cadence a scheduled job's ``meta`` mirrors (interval seconds or cron string); neither raises."""
    if meta.get("interval"):
        return normalize_schedule(meta["interval"])
    if meta.get("cron_string"):
        return normalize_schedule(meta["cron_string"])
    raise ValueError(
        f"Schedule {name!r} has no 'interval' or 'cron_string' in its job meta; cannot reconstruct its schedule"
    )


def _stored(name: str, job: Any, next_run_at_ts: float | None) -> StoredSchedule:
    return StoredSchedule(
        name=name,
        args=list(job.args),
        kwargs=dict(job.kwargs),
        cadence=_cadence_from_meta(name, dict(job.meta or {})),
        enabled=True,
        next_run_at_ts=next_run_at_ts,
    )


def _fetch(scheduler: Any, r: Any, name: str) -> Any | None:
    """The scheduled job named ``name``, or ``None`` when the scheduler holds none."""
    if name not in scheduler:
        return None
    try:
        return scheduler.job_class.fetch(name, connection=r)
    except NoSuchJobError:
        return None


class RqScheduleStore:
    """The rq-scheduler store of RQ's schedules."""

    @property
    def relative_intervals(self) -> bool:
        """``False``: the rq scheduler re-arms an interval on its own clock only."""
        return False

    @property
    def holds_disabled(self) -> bool:
        """``False``: the rq scheduler holds no disabled job."""
        return False

    async def write(self, record: ScheduleRecord) -> None:
        """Create or replace the scheduled job of ``record``'s name.

        The rq scheduler holds no disabled schedule, so a record with ``enabled=False`` raises
        ``ValueError`` rather than being written as an active one.
        """
        if not record.enabled:
            raise ValueError(f"schedule {record.name!r} has enabled=False; the rq backend holds no disabled schedule")
        kwargs = fired_kwargs(record, rq_settings().tool_name_arg)
        async with _sync_redis() as r:
            await apply_normalized_schedule(
                scheduler_for(r), record.cadence, tool_execution, record.args, kwargs, record.name
            )

    async def exists(self, name: str) -> bool:
        """Whether the scheduler's zset holds ``name``."""
        async with _sync_redis() as r:
            return await asyncio.to_thread(r.zscore, rq_settings().rq_scheduler_zset, name) is not None

    async def read(self, name: str) -> StoredSchedule | None:
        """The scheduled job of ``name``, its next run the zset score."""
        async with _sync_redis() as r:
            scheduler = scheduler_for(r)

            def _read() -> StoredSchedule | None:
                job = _fetch(scheduler, r, name)
                if job is None:
                    return None
                score = r.zscore(rq_settings().rq_scheduler_zset, name)
                return _stored(name, job, None if score is None else float(score))

            return await asyncio.to_thread(_read)

    async def read_all(self) -> list[StoredSchedule]:
        """Every scheduled job, each with its next run (the scheduler reports a naive time as UTC)."""
        async with _sync_redis() as r:
            scheduler = scheduler_for(r)

            def _read() -> list[StoredSchedule]:
                stored: list[StoredSchedule] = []
                pairs = cast("list[tuple[Any, datetime]]", scheduler.get_jobs(with_times=True))
                for job, next_run in pairs:
                    run_at = next_run.replace(tzinfo=UTC) if next_run.tzinfo is None else next_run
                    stored.append(_stored(job.id, job, run_at.timestamp()))
                return stored

            return await asyncio.to_thread(_read)

    async def delete(self, name: str) -> bool:
        """Remove the zset member, then the job hash."""
        settings = rq_settings()
        async with _sync_redis() as r:

            def _delete() -> bool:
                if not r.zrem(settings.rq_scheduler_zset, name):
                    return False
                r.delete(settings.rq_job_key(name))
                return True

            return await asyncio.to_thread(_delete)

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        """Never called: the store holds no disabled schedule (:attr:`holds_disabled`), so it raises."""
        raise UnsupportedBackendTool(RqBackend.label, "a disabled schedule")

    async def run_now(self, name: str) -> bool:
        """Enqueue the scheduled job at once; ``Scheduler.enqueue_job`` re-arms its recurrence itself."""
        async with _sync_redis() as r:
            scheduler = scheduler_for(r)

            def _run() -> bool:
                job = _fetch(scheduler, r, name)
                if job is None:
                    return False
                scheduler.enqueue_job(job)
                return True

            return await asyncio.to_thread(_run)

    async def update(
        self,
        name: str,
        *,
        cadence: IntervalCadence | CrontabCadence | None,
        next_run_at_ts: float | None,
    ) -> bool:
        """Re-create the job under a new cadence, or move its next run in place.

        A new cadence cancels the job and re-creates it under the same id with its args, kwargs
        and custom meta; the replaced kind's meta key is dropped, so it reads back as one kind. A
        new interval runs first at ``next_run_at_ts``, or now when none is given; a new crontab
        runs first at its next matching time, then ``next_run_at_ts`` moves that run. A next run
        alone moves the job's next execution in place (for a crontab, only that one run).
        """
        explicit = None if next_run_at_ts is None else datetime.fromtimestamp(next_run_at_ts, tz=UTC)
        async with _sync_redis() as r:
            scheduler = scheduler_for(r)

            def _update() -> bool:
                job = _fetch(scheduler, r, name)
                if job is None:
                    return False
                if cadence is None:
                    assert explicit is not None  # noqa: S101 the contract gives at least one change
                    scheduler.change_execution_time(job, explicit)
                    return True
                # Computed before the cancel, so an interval RQ cannot represent raises while the
                # prior schedule is still intact.
                every = interval_seconds(cadence) if isinstance(cadence, IntervalCadence) else None
                meta = {k: v for k, v in dict(job.meta or {}).items() if k not in ("interval", "cron_string")}
                scheduler.cancel(name)
                if every is not None:
                    scheduler.schedule(
                        scheduled_time=explicit or datetime.now(UTC),
                        func=tool_execution,
                        args=job.args,
                        kwargs=job.kwargs,
                        interval=every,
                        id=name,
                        meta={**meta, "interval": every},
                    )
                    return True
                assert isinstance(cadence, CrontabCadence)  # noqa: S101 narrows the two-kind union
                scheduler.cron(
                    cadence.expression,
                    func=tool_execution,
                    args=job.args,
                    kwargs=job.kwargs,
                    id=name,
                    meta={**meta, "cron_string": cadence.expression},
                )
                if explicit is not None:
                    scheduler.change_execution_time(job, explicit)
                return True

            return await asyncio.to_thread(_update)
