from typing import Any

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


def parse_crontab_expr(expr: str) -> dict[str, Any]:
    """
    Accepts standard 5-field cron: M H DOM MON DOW
    If a 6-field expression (with seconds) is passed, ignores the first field.
    """
    parts = expr.split()
    if len(parts) == 6:  # seconds minute hour dom mon dow
        parts = parts[1:]
    if len(parts) != 5:
        raise ValueError(f"Invalid crontab expression (need 5 fields): {expr!r}")
    minute, hour, day_of_month, month_of_year, day_of_week = parts
    return {
        "type": "crontab",
        "minute": minute,
        "hour": hour,
        "day_of_month": day_of_month,
        "month_of_year": month_of_year,
        "day_of_week": day_of_week,
    }


def _interval_from_seconds(value: int | float) -> dict[str, Any]:
    """A raw numeric seconds count as a RedBeat interval (raises on ``<= 0``)."""
    if value <= 0:
        raise ValueError("interval 'every' must be > 0 seconds")
    return {"__type__": "interval", "every": float(value), "relative": False}


def _canonical_schedule(spec: dict[str, Any]) -> dict[str, Any]:
    """An already-``__type__`` dict, float-coercing ``every`` and defaulting
    ``relative`` for an interval (raises on ``<= 0``)."""
    out = dict(spec)
    if out["__type__"] == "interval":
        out["every"] = float(out["every"])
        if out["every"] <= 0:
            raise ValueError("interval 'every' must be > 0 seconds")
        out.setdefault("relative", False)
    return out


def _interval_from_friendly(spec: dict[str, Any]) -> dict[str, Any]:
    """A friendly interval schema (``every``/``run_every`` times the ``period``
    multiplier) as a RedBeat interval (raises on missing/unsupported/``<= 0``)."""
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
    return {"__type__": "interval", "every": every_sec, "relative": bool(spec.get("relative", False))}


def _crontab_from_friendly(spec: dict[str, Any]) -> dict[str, Any]:
    """A friendly crontab schema as RedBeat crontab: an ``expression`` parsed with
    per-field overrides, else the per-field values defaulting to ``*``."""
    if "expression" in spec:
        base = parse_crontab_expr(spec["expression"])
        base["__type__"] = base.pop("type", "crontab")
        # allow explicit field overrides
        base["minute"] = spec.get("minute", base["minute"])
        base["hour"] = spec.get("hour", base["hour"])
        base["day_of_month"] = spec.get("day_of_month", base["day_of_month"])
        base["month_of_year"] = spec.get("month_of_year", base["month_of_year"])
        base["day_of_week"] = spec.get("day_of_week", base["day_of_week"])
        return base
    return {
        "__type__": "crontab",
        "minute": spec.get("minute", "*"),
        "hour": spec.get("hour", "*"),
        "day_of_month": spec.get("day_of_month", "*"),
        "month_of_year": spec.get("month_of_year", "*"),
        "day_of_week": spec.get("day_of_week", "*"),
    }


def normalize_schedule(s: int | float | str | dict[str, Any]) -> dict[str, Any]:
    """
    Normalize to RedBeat-native JSON:
      - interval: {"__type__":"interval","every":<seconds: float>,"relative":False|True}
      - crontab : {"__type__":"crontab","minute":...,"hour":...,"day_of_month":...,
                   "month_of_year":...,"day_of_week":...}
    """
    # numeric => interval seconds
    if isinstance(s, (int, float)):
        return _interval_from_seconds(s)

    # already-canonical dict
    if isinstance(s, dict) and s.get("__type__") in {"interval", "crontab"}:
        return _canonical_schedule(s)

    # bare string => crontab expression
    if isinstance(s, str):
        base = parse_crontab_expr(s)  # expects {"type":"crontab", ...}
        base["__type__"] = base.pop("type", "crontab")
        return base

    # dict => translate friendly schema to RedBeat
    if isinstance(s, dict):
        t = s.get("type")
        if t == "interval":
            return _interval_from_friendly(s)
        if t == "crontab":
            return _crontab_from_friendly(s)

    raise ValueError(f"Unsupported schedule format: {s!r}")
