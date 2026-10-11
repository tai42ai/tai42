"""Schedule-shape helpers over the contract's ``ScheduleCadence``.

The schedule hash stores the cadence as its JSON dump (field ``schedule``) and the compact
``cron_or_interval`` form arq fires from (interval seconds or a 5-field crontab string).
:func:`read_stored_cadence` reads the stored dump back and refuses a hash written in a
format this backend no longer reads (:class:`ScheduleStoreFormatError`).
"""

from __future__ import annotations

from datetime import UTC, datetime

from croniter import croniter
from pydantic import TypeAdapter, ValidationError
from tai42_contract.backend import CrontabCadence, IntervalCadence, ScheduleCadence
from tai42_contract.errors import ErrorKind

_CADENCE: TypeAdapter[IntervalCadence | CrontabCadence] = TypeAdapter(ScheduleCadence)


class ScheduleStoreFormatError(RuntimeError):
    """A schedule hash holds a ``schedule`` field this backend does not read.

    The stored state conflicts with what this backend reads — deterministic, so no default
    retry policy re-runs it. The message names the schedule and the key pattern to delete.
    """

    __tai_error_kind__ = ErrorKind.CONFLICT


def read_stored_cadence(name: str, raw: bytes | str | None, pattern: str) -> IntervalCadence | CrontabCadence:
    """The cadence stored in a schedule hash's ``schedule`` field.

    A missing field or one that is not a ``ScheduleCadence`` dump raises
    :class:`ScheduleStoreFormatError` naming ``pattern`` (the schedule-key glob to delete).
    """
    cause: ValidationError | None = None
    if raw is not None:
        try:
            return _CADENCE.validate_json(raw)
        except ValidationError as exc:
            cause = exc
    raise ScheduleStoreFormatError(
        f"arq schedule {name!r} is stored in a format this backend no longer reads; delete the keys "
        f"matching {pattern!r} and re-create the schedules"
    ) from cause


def refuse_relative(cadence: IntervalCadence | CrontabCadence) -> None:
    """Raise for a relative interval: the arq scheduler cannot honour ``relative=True``."""
    if isinstance(cadence, IntervalCadence) and cadence.relative:
        raise ValueError("the arq backend does not support relative interval schedules (relative=True)")


def derive_cron_or_interval(cadence: IntervalCadence | CrontabCadence) -> int | float | str:
    """Reduce a cadence to its compact stored form.

    An interval becomes its seconds value (int when whole); a crontab becomes the standard
    5-field expression string.
    """
    if isinstance(cadence, IntervalCadence):
        every = cadence.every
        return int(every) if every.is_integer() else every
    return f"{cadence.minute} {cadence.hour} {cadence.day_of_month} {cadence.month_of_year} {cadence.day_of_week}"


def parse_cron_or_interval(raw: str) -> int | float | str:
    """Parse the stored compact form back into seconds (numeric) or a crontab string (anything non-numeric)."""
    try:
        value = float(raw)
    except ValueError:
        return raw
    return int(value) if value.is_integer() else value


def next_run_after(cron_or_interval: int | float | str, now: datetime | None = None) -> tuple[float, float]:
    """Compute ``(defer_by_seconds, next_run_timestamp)`` for the first run strictly after ``now`` (UTC)."""
    now = now or datetime.now(UTC)
    if isinstance(cron_or_interval, str):
        next_time = croniter(cron_or_interval, now).get_next(datetime)
        return (next_time - now).total_seconds(), next_time.timestamp()
    if isinstance(cron_or_interval, int | float):
        return float(cron_or_interval), now.timestamp() + cron_or_interval
    raise ValueError("cron_or_interval must be cron str or interval number")
