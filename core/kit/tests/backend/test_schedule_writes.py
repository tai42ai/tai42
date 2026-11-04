"""What every schedule write door shares: the cadence check and the stored job kwargs."""

from __future__ import annotations

import pytest
from tai42_contract.backend import CrontabCadence, IntervalCadence, ScheduleRecord

from tai42_kit.backend import check_cadence_supported, fired_kwargs

from .branch_fakes import FakeScheduleWriter


def test_a_writer_without_relative_intervals_refuses_a_relative_cadence() -> None:
    writer = FakeScheduleWriter(relative_intervals=False)
    with pytest.raises(ValueError, match="the acme backend does not support relative interval schedules"):
        check_cadence_supported(writer, IntervalCadence(every=5, relative=True), "acme")


@pytest.mark.parametrize("cadence", [IntervalCadence(every=5), CrontabCadence()])
def test_a_writer_without_relative_intervals_takes_every_other_cadence(
    cadence: IntervalCadence | CrontabCadence,
) -> None:
    check_cadence_supported(FakeScheduleWriter(relative_intervals=False), cadence, "acme")


def test_a_writer_with_relative_intervals_takes_a_relative_cadence() -> None:
    writer = FakeScheduleWriter(relative_intervals=True)
    check_cadence_supported(writer, IntervalCadence(every=5, relative=True), "acme")


def test_the_fired_kwargs_carry_the_tool_under_the_tool_name_argument() -> None:
    record = ScheduleRecord(name="n", tool="t", args=[], kwargs={"x": 1}, cadence=CrontabCadence(), enabled=True)
    assert fired_kwargs(record, "backend_tool_name") == {"x": 1, "backend_tool_name": "t"}
    assert record.kwargs == {"x": 1}
