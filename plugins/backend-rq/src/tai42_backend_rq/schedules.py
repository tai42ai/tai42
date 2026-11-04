"""Writing a typed cadence into RQ: the scheduler factory and the one schedule writer.

``apply_normalized_schedule`` is shared by the create and import paths so every schedule
lands the same way, mirroring the interval seconds or cron string into the job ``meta``
for read-back. RQ cannot honour a relative interval: every write door refuses one first
(:func:`~tai42_kit.backend.check_cadence_supported` over
:class:`~tai42_backend_rq.schedule_store.RqScheduleStore`).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from rq_scheduler import Scheduler
from tai42_contract.backend import CrontabCadence, IntervalCadence

from tai42_backend_rq.queue import TaskBudgetQueue
from tai42_backend_rq.settings import rq_settings


def scheduler_for(connection: Any) -> Scheduler:
    """The scheduler over the configured queue, moving due jobs onto a :class:`TaskBudgetQueue`."""
    return Scheduler(queue_name=rq_settings().queue_name, connection=connection, queue_class=TaskBudgetQueue)


def interval_seconds(cadence: IntervalCadence) -> int:
    """The whole-second interval of an interval cadence.

    The RQ scheduler re-arms an interval as ``int(interval)`` seconds, so a fractional or
    sub-second value is rejected loudly rather than silently truncated.
    """
    every = cadence.every
    seconds = int(every)
    if seconds != every or seconds < 1:
        raise ValueError(
            f"The RQ scheduler recurs on whole seconds (>= 1); interval 'every'={every!r} cannot be represented"
        )
    return seconds


async def apply_normalized_schedule(
    scheduler: Any,
    cadence: IntervalCadence | CrontabCadence,
    func: Any,
    args: list[Any],
    kwargs: dict[str, Any],
    schedule_name: str,
) -> None:
    """Create a scheduled job named ``schedule_name`` from a typed cadence.

    An interval becomes a recurring ``scheduler.schedule`` call (whole seconds), a crontab a
    ``scheduler.cron`` call; the interval seconds or cron string is mirrored into the job
    ``meta`` for read-back. The scheduler calls are blocking, so they run off the event loop.

    An interval schedule's FIRST run is one interval out, not at creation: an
    interval fires on its cadence, never the instant it is registered. Firing at
    creation would enqueue-and-reschedule the job immediately, and that reschedule
    (its zset re-add) races a delete issued right after creation, resurrecting a
    schedule the caller just removed. Deferring the first run by one interval
    means a freshly registered schedule holds a stable, unfired zset entry that a
    delete removes cleanly. A crontab schedule is already anchored to its next
    matching wall-clock time, so it never fires at creation.
    """
    meta_data: dict[str, Any] = {}

    if isinstance(cadence, IntervalCadence):
        interval_val = interval_seconds(cadence)
        meta_data["interval"] = interval_val

        await asyncio.to_thread(
            lambda: scheduler.schedule(
                scheduled_time=datetime.now(UTC) + timedelta(seconds=interval_val),
                func=func,
                args=args,
                kwargs=kwargs,
                interval=interval_val,
                id=schedule_name,
                meta=meta_data,
            )
        )
    else:
        cron = cadence.expression
        meta_data["cron_string"] = cron

        await asyncio.to_thread(
            lambda: scheduler.cron(
                cron,
                func=func,
                args=args,
                kwargs=kwargs,
                id=schedule_name,
                meta=meta_data,
            )
        )
