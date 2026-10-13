"""The scheduling protocol's models: cadences, the record, the list row, the import report, the create body."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from tai42_contract.backend import (
    SCHEDULE_BRANCH_SUFFIX,
    CrontabCadence,
    IntervalCadence,
    ScheduleCadence,
    ScheduleCreate,
    ScheduleImportReport,
    ScheduleListRow,
    ScheduleRecord,
)
from tai42_contract.schema_export import build_document_schemas

_CADENCE: TypeAdapter[IntervalCadence | CrontabCadence] = TypeAdapter(ScheduleCadence)


def test_both_cadences_parse_through_the_discriminator() -> None:
    interval = _CADENCE.validate_python({"kind": "interval", "every": 30})
    crontab = _CADENCE.validate_python({"kind": "crontab", "minute": "0", "hour": "9"})
    assert isinstance(interval, IntervalCadence)
    assert interval == IntervalCadence(every=30.0)
    assert interval.relative is False
    assert isinstance(crontab, CrontabCadence)
    assert crontab == CrontabCadence(minute="0", hour="9")
    assert crontab.day_of_week == "*"


def test_the_discriminator_refuses_an_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        _CADENCE.validate_python({"kind": "solar", "every": 1})


def test_a_redbeat_tagged_definition_is_not_a_cadence() -> None:
    with pytest.raises(ValidationError):
        _CADENCE.validate_python({"__type__": "interval", "every": 30, "relative": False})


@pytest.mark.parametrize("every", [0, -1, -0.5])
def test_an_interval_must_be_positive(every: float) -> None:
    with pytest.raises(ValidationError):
        IntervalCadence(every=every)


def test_a_five_field_expression_maps_in_order() -> None:
    cadence = CrontabCadence.model_validate({"expression": "*/5 1 2 3 4"})
    assert cadence.model_dump() == {
        "kind": "crontab",
        "minute": "*/5",
        "hour": "1",
        "day_of_month": "2",
        "month_of_year": "3",
        "day_of_week": "4",
    }


def test_a_six_field_expression_drops_the_leading_seconds() -> None:
    cadence = _CADENCE.validate_python({"kind": "crontab", "expression": "30 0 9 * * 1"})
    assert cadence == CrontabCadence(minute="0", hour="9", day_of_week="1")


@pytest.mark.parametrize(("expression", "count"), [("* * * *", 4), ("* * * * * * *", 7)])
def test_an_expression_of_another_length_is_refused(expression: str, count: int) -> None:
    with pytest.raises(ValidationError, match=rf"a crontab expression has 5 fields \(6 with seconds\); got {count}"):
        CrontabCadence.model_validate({"expression": expression})


def test_an_expression_beside_a_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        CrontabCadence.model_validate({"expression": "* * * * *", "minute": "0"})


def test_a_crontab_dumps_its_fields_only() -> None:
    dumped = CrontabCadence.model_validate({"expression": "0 9 * * *"}).model_dump(mode="json")
    assert "expression" not in dumped


def _record(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "nightly",
        "tool": "echo",
        "args": [],
        "kwargs": {"text": "hi"},
        "cadence": {"kind": "interval", "every": 60},
        "enabled": True,
        **overrides,
    }


def test_a_record_round_trips() -> None:
    record = ScheduleRecord.model_validate(_record())
    assert ScheduleRecord.model_validate(record.model_dump(mode="json")) == record


def test_a_record_refuses_an_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ScheduleRecord.model_validate(_record(schedule={"__type__": "interval", "every": 60}))


def test_a_record_without_a_tool_is_refused() -> None:
    row = _record()
    del row["tool"]
    with pytest.raises(ValidationError):
        ScheduleRecord.model_validate(row)


def test_a_list_row_carries_no_kwargs() -> None:
    row = {"name": "n", "tool": "echo", "cadence": {"kind": "interval", "every": 5}, "enabled": True}
    assert ScheduleListRow.model_validate(row).error is None
    with pytest.raises(ValidationError):
        ScheduleListRow.model_validate({**row, "kwargs": {}})
    with_error = ScheduleListRow.model_validate({**row, "error": {"message": "lost", "at_iso": "2026-01-01T00:00:00"}})
    assert with_error.error is not None
    assert with_error.error.message == "lost"


def test_an_import_report_refuses_an_unknown_key() -> None:
    report: dict[str, Any] = {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 2, "errors": []}
    assert ScheduleImportReport.model_validate(report).skipped_existing == 2
    with pytest.raises(ValidationError):
        ScheduleImportReport.model_validate({**report, "extra": 1})
    with pytest.raises(ValidationError):
        ScheduleImportReport.model_validate({key: value for key, value in report.items() if key != "skipped"})


def test_create_accepts_a_base_tool_and_a_cadence() -> None:
    body = ScheduleCreate.model_validate(
        {"tool_name": "echo", "cadence": {"kind": "crontab", "minute": "0"}, "schedule_name": "daily"}
    )
    assert body.cadence == CrontabCadence(minute="0")
    assert ScheduleCreate.model_validate({"tool_name": "echo"}).cadence is None


def test_create_refuses_a_name_without_a_cadence() -> None:
    with pytest.raises(ValidationError, match="schedule_name needs a cadence; a run-once schedule has no name"):
        ScheduleCreate.model_validate({"tool_name": "echo", "schedule_name": "daily"})


def test_create_refuses_a_vehicle_tool_name() -> None:
    with pytest.raises(ValidationError, match="tool_name names a schedule vehicle; pass the base tool and a cadence"):
        ScheduleCreate.model_validate(
            {"tool_name": f"echo{SCHEDULE_BRANCH_SUFFIX}", "cadence": {"kind": "interval", "every": 5}}
        )


def test_create_refuses_schedule_kwargs() -> None:
    with pytest.raises(ValidationError):
        ScheduleCreate.model_validate({"tool_name": "echo", "schedule_kwargs": {"cron": "* * * * *"}})


def test_create_refuses_a_contract_jq_without_an_execution_key() -> None:
    with pytest.raises(ValidationError, match="requires an execution_key"):
        ScheduleCreate.model_validate({"tool_name": "echo", "start_expr": {"content": "."}})


def _cadence_schema(document: dict[str, Any]) -> dict[str, Any]:
    cadence = document["properties"]["cadence"]
    for option in cadence.get("anyOf", [cadence]):
        if "oneOf" in option:
            return option
    raise AssertionError(f"no oneOf on cadence: {cadence}")


@pytest.mark.parametrize("name", ["ScheduleCreate", "ScheduleListRow", "ScheduleRecord"])
def test_the_served_documents_discriminate_the_cadence_on_kind(name: str) -> None:
    document = build_document_schemas()["documents"][name]
    cadence = _cadence_schema(document)
    assert cadence["discriminator"]["propertyName"] == "kind"
    assert set(cadence["discriminator"]["mapping"]) == {"interval", "crontab"}
