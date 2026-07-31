"""Schedule shapes: the portable :class:`ScheduleRecord` and the one code path
that writes a canonical schedule into the RQ scheduler.

``ScheduleRecord`` is the backend-neutral, JSON-serializable backup form.
``apply_normalized_schedule`` is shared by the create, update, and import paths
so every schedule lands the same way, mirroring the interval seconds or cron
string into the job ``meta`` for read-back.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ScheduleRecord(BaseModel):
    """One schedule in a backend-neutral, JSON-serializable form: ``name``, the
    scheduled call's ``args``/``kwargs``, the canonical interval-or-crontab
    ``schedule`` dict, and the ``enabled`` flag. Round-trips through a backup
    document via ``model_dump`` / ``model_validate``."""

    name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any]
    enabled: bool

    @model_validator(mode="after")
    def _check_schedule_kind(self) -> ScheduleRecord:
        kind = self.schedule.get("__type__")
        if kind not in {"interval", "crontab"}:
            raise ValueError(f"schedule '__type__' must be 'interval' or 'crontab', got {kind!r}")
        if kind == "interval":
            every = self.schedule.get("every")
            # bool is an int subclass; a boolean 'every' is not a valid period.
            if not isinstance(every, int | float) or isinstance(every, bool):
                raise ValueError(f"interval schedule requires numeric 'every', got {every!r}")
            relative = self.schedule.get("relative")
            if relative is not None and not isinstance(relative, bool):
                raise ValueError(f"interval 'relative' must be a bool, got {relative!r}")
        else:  # crontab
            missing = [
                field
                for field in ("minute", "hour", "day_of_month", "month_of_year", "day_of_week")
                if field not in self.schedule
            ]
            if missing:
                raise ValueError(f"crontab schedule missing required field(s): {missing}")
        return self


def crontab_string(norm: dict[str, Any]) -> str:
    """The 5-field cron expression of a canonical crontab dict."""
    return f"{norm['minute']} {norm['hour']} {norm['day_of_month']} {norm['month_of_year']} {norm['day_of_week']}"


def interval_seconds(norm: dict[str, Any]) -> int:
    """The whole-second interval of a canonical interval dict. The RQ scheduler
    re-arms an interval as ``int(interval)`` seconds, so a fractional or
    sub-second value is rejected loudly rather than silently truncated."""
    every = float(norm["every"])
    seconds = int(every)
    if seconds != every or seconds < 1:
        raise ValueError(
            f"The RQ scheduler recurs on whole seconds (>= 1); interval 'every'={every!r} cannot be represented"
        )
    return seconds


async def apply_normalized_schedule(
    scheduler: Any,
    norm: dict[str, Any],
    func: Any,
    args: list[Any],
    kwargs: dict[str, Any],
    schedule_name: str,
) -> None:
    """Create a scheduled job named ``schedule_name`` from a canonical schedule dict.

    An interval schedule becomes a recurring ``scheduler.schedule`` call (whole
    seconds), a crontab schedule a ``scheduler.cron`` call; the interval seconds
    or cron string is mirrored into the job ``meta`` for read-back. Any other
    ``__type__`` raises. The scheduler calls are blocking, so they run off the
    event loop.
    """
    meta_data: dict[str, Any] = {}

    if norm["__type__"] == "interval":
        interval_val = interval_seconds(norm)
        meta_data["interval"] = interval_val

        await asyncio.to_thread(
            lambda: scheduler.schedule(
                scheduled_time=datetime.now(UTC),
                func=func,
                args=args,
                kwargs=kwargs,
                interval=interval_val,
                id=schedule_name,
                meta=meta_data,
            )
        )
    elif norm["__type__"] == "crontab":
        cron = crontab_string(norm)
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
    else:
        raise ValueError(f"Unsupported schedule type: {norm['__type__']}")
