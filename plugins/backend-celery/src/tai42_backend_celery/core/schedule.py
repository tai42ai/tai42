"""Backend-neutral schedule record used by the export/import backup round trip."""

from __future__ import annotations

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
