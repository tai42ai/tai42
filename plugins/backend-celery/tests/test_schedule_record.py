"""ScheduleRecord validation."""

from __future__ import annotations

import pytest

from tai42_backend_celery.core.schedule import ScheduleRecord


def test_interval_record_round_trips() -> None:
    record = ScheduleRecord(
        name="job",
        kwargs={"backend_tool_name": "my_tool"},
        schedule={"__type__": "interval", "every": 30.0, "relative": False},
        enabled=True,
    )
    assert ScheduleRecord.model_validate(record.model_dump()) == record


def test_unknown_schedule_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="'interval' or 'crontab'"):
        ScheduleRecord(name="job", schedule={"__type__": "hourly"}, enabled=True)


def test_boolean_every_is_rejected() -> None:
    with pytest.raises(ValueError, match="numeric 'every'"):
        ScheduleRecord(name="job", schedule={"__type__": "interval", "every": True}, enabled=True)


def test_non_bool_relative_is_rejected() -> None:
    with pytest.raises(ValueError, match="'relative' must be a bool"):
        ScheduleRecord(name="job", schedule={"__type__": "interval", "every": 5, "relative": "yes"}, enabled=True)


def test_crontab_missing_fields_are_named() -> None:
    with pytest.raises(ValueError, match="missing required field"):
        ScheduleRecord(name="job", schedule={"__type__": "crontab", "minute": "0"}, enabled=True)
