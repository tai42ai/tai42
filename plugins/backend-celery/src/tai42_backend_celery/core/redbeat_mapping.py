"""The mapping between the contract's ``ScheduleCadence`` and RedBeat's stored schedule shape.

RedBeat stores an entry's schedule as its own JSON (``RedBeatJSONEncoder``): an interval as
``{"__type__": "interval", "every": <seconds>, "relative": <bool>}`` and a crontab as
``{"__type__": "crontab", "minute", "hour", "day_of_week", "day_of_month", "month_of_year"}``
(keys in that order). :func:`to_redbeat` emits exactly that dict, so a definition this backend
writes itself is byte-identical to one RedBeat's encoder writes; :func:`from_redbeat` reads
either back as the typed cadence; :func:`to_celery_schedule` builds the Celery schedule object
a ``RedBeatSchedulerEntry`` saves.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from celery.schedules import crontab, schedule
from tai42_contract.backend import CrontabCadence, IntervalCadence


def to_redbeat(cadence: IntervalCadence | CrontabCadence) -> dict[str, Any]:
    """The RedBeat JSON definition of ``cadence``.

    An interval always carries ``relative``: RedBeat's decoder reads ``d["relative"]``.
    """
    if isinstance(cadence, IntervalCadence):
        return {"__type__": "interval", "every": cadence.every, "relative": cadence.relative}
    return {
        "__type__": "crontab",
        "minute": cadence.minute,
        "hour": cadence.hour,
        "day_of_week": cadence.day_of_week,
        "day_of_month": cadence.day_of_month,
        "month_of_year": cadence.month_of_year,
    }


def from_redbeat(stored: Mapping[str, Any]) -> IntervalCadence | CrontabCadence:
    """The typed cadence of a RedBeat JSON schedule definition; an unknown ``__type__`` raises."""
    tag = stored.get("__type__")
    fields = {key: value for key, value in stored.items() if key != "__type__"}
    if tag == "interval":
        return IntervalCadence(**fields)
    if tag == "crontab":
        return CrontabCadence(**fields)
    raise ValueError(f"RedBeat definition carries an unknown schedule type {tag!r}")


def to_celery_schedule(cadence: IntervalCadence | CrontabCadence) -> schedule | crontab:
    """The Celery schedule object a ``RedBeatSchedulerEntry`` saves for ``cadence``."""
    if isinstance(cadence, IntervalCadence):
        return schedule(run_every=timedelta(seconds=cadence.every), relative=cadence.relative)
    return crontab(
        minute=cadence.minute,
        hour=cadence.hour,
        day_of_week=cadence.day_of_week,
        day_of_month=cadence.day_of_month,
        month_of_year=cadence.month_of_year,
    )
