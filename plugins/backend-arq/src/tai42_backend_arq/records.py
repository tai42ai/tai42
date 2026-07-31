"""Portable schedule records and schedule-shape helpers.

:class:`ScheduleRecord` is the JSON-safe document the export/import backup round
trip emits and consumes — the durable definition only, never runtime state. The
helpers convert between the normalized schedule dict, the compact
``cron_or_interval`` form stored in the schedule hash (interval seconds or a
5-field crontab string), and the next-run timing derived from it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from croniter import croniter
from pydantic import BaseModel, Field, model_validator


class ScheduleRecord(BaseModel):
    """The durable definition of one schedule; runtime state (job id, timestamps,
    abort markers) is absent and re-derived on import. ``kwargs`` carries the
    queued call's keyword arguments verbatim (including the tool-name key)."""

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


def derive_cron_or_interval(norm: dict[str, Any]) -> int | float | str:
    """Reduce a normalized schedule dict to its compact stored form.

    An interval becomes its seconds value (int when whole); a crontab becomes
    the standard 5-field expression string.
    """
    if norm["__type__"] == "interval":
        every = float(norm["every"])
        return int(every) if every.is_integer() else every
    if norm["__type__"] == "crontab":
        return f"{norm['minute']} {norm['hour']} {norm['day_of_month']} {norm['month_of_year']} {norm['day_of_week']}"
    raise ValueError(f"Unsupported schedule type: {norm['__type__']}")


def parse_cron_or_interval(raw: str) -> int | float | str:
    """Parse the stored compact form back into seconds (numeric) or a crontab
    string (anything non-numeric)."""
    try:
        value = float(raw)
    except ValueError:
        return raw
    return int(value) if value.is_integer() else value


def next_run_after(cron_or_interval: int | float | str, now: datetime | None = None) -> tuple[float, float]:
    """Compute ``(defer_by_seconds, next_run_timestamp)`` for the first run
    strictly after ``now`` (UTC)."""
    now = now or datetime.now(UTC)
    if isinstance(cron_or_interval, str):
        next_time = croniter(cron_or_interval, now).get_next(datetime)
        return (next_time - now).total_seconds(), next_time.timestamp()
    if isinstance(cron_or_interval, int | float):
        return float(cron_or_interval), now.timestamp() + cron_or_interval
    raise ValueError("cron_or_interval must be cron str or interval number")
