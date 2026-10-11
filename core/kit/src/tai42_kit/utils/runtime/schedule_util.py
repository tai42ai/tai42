"""Parse friendly interval and crontab specs into the contract's ``ScheduleCadence``."""

from collections.abc import Mapping
from typing import Any

from pydantic import TypeAdapter
from tai42_contract.backend import CrontabCadence, IntervalCadence, ScheduleCadence

_PERIODS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
}

_CRONTAB_FIELDS = ("minute", "hour", "day_of_month", "month_of_year", "day_of_week")

_CADENCE: TypeAdapter[IntervalCadence | CrontabCadence] = TypeAdapter(ScheduleCadence)


def parse_crontab_expr(expr: str) -> CrontabCadence:
    """A standard 5-field cron expression (M H DOM MON DOW) as a :class:`CrontabCadence`.

    A 6-field expression (with a leading seconds field) drops the seconds; any other count raises.
    """
    return CrontabCadence.model_validate({"expression": expr})


def _interval_from_seconds(value: float) -> IntervalCadence:
    """A raw numeric seconds count as an interval (raises on ``<= 0``)."""
    if value <= 0:
        raise ValueError("interval 'every' must be > 0 seconds")
    return IntervalCadence(every=float(value))


def _canonical_schedule(spec: Mapping[str, Any]) -> IntervalCadence | CrontabCadence:
    """A ``kind``-tagged mapping validated as a :data:`ScheduleCadence`."""
    return _CADENCE.validate_python(dict(spec))


def _interval_from_friendly(spec: Mapping[str, Any]) -> IntervalCadence:
    """A friendly interval (``every``/``run_every`` times the ``period`` multiplier).

    Raises on missing/unsupported/``<= 0``.
    """
    every = spec.get("every") or spec.get("run_every")
    if every is None:
        raise ValueError("interval schedule requires 'every'")
    period = (spec.get("period") or "seconds").strip().lower()
    mult = _PERIODS.get(period)
    if mult is None:
        raise ValueError(f"Unsupported period: {period!r}")
    every_sec = float(every) * mult
    if every_sec <= 0:
        raise ValueError("interval 'every' must be > 0 seconds")
    return IntervalCadence(every=every_sec, relative=bool(spec.get("relative", False)))


def _crontab_from_friendly(spec: Mapping[str, Any]) -> CrontabCadence:
    """A friendly crontab: an ``expression`` with per-field overrides, else the fields (each ``*`` by default)."""
    base = parse_crontab_expr(spec["expression"]).model_dump() if "expression" in spec else {}
    fields = {field: spec.get(field, base.get(field, "*")) for field in _CRONTAB_FIELDS}
    return CrontabCadence(**fields)


def normalize_schedule(
    s: int | float | str | Mapping[str, Any] | IntervalCadence | CrontabCadence,
) -> IntervalCadence | CrontabCadence:
    """Normalize a friendly schedule spec to the contract's typed cadence.

    - a number → an interval of that many seconds;
    - a string → a crontab from its expression;
    - a cadence → itself;
    - a mapping with ``kind`` → validated as a :data:`ScheduleCadence`;
    - a mapping with ``type`` ``"interval"`` (``every``/``run_every`` times ``period``) or ``"crontab"``
      (``expression`` plus per-field overrides, or the fields alone).

    Anything else raises ``ValueError``.
    """
    if isinstance(s, IntervalCadence | CrontabCadence):
        return s
    if isinstance(s, int | float):
        return _interval_from_seconds(s)
    if isinstance(s, str):
        return parse_crontab_expr(s)
    if isinstance(s, Mapping):
        if "kind" in s:
            return _canonical_schedule(s)
        kind = s.get("type")
        if kind == "interval":
            return _interval_from_friendly(s)
        if kind == "crontab":
            return _crontab_from_friendly(s)
    raise ValueError(f"unsupported schedule spec: {type(s).__name__}")
