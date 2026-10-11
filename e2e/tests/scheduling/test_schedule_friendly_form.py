"""A typed crontab cadence end-to-end: a BASE tool plus ``cadence`` ``{kind: "crontab", ...}``.

``test_schedule_task`` proves an interval cadence fires periodically. This proves the
crontab kind: the door dispatches the ``e2e_record_schedule_task`` vehicle with the dumped
cadence, a real backend schedule is registered, and the cross-worker listing serves it as a
typed ``ScheduleListRow`` (the fired tool and the cadence, never the stored kwargs). A
crontab cadence fires on minute boundaries, so this asserts registration and unscheduling
rather than a fire."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs(
    "kind:backend",
    "probe-tools",
    "topology:replicas",
    "setting:extension:schedule_task",
)


async def test_friendly_cron_form_registers_and_unschedules_cross_worker(
    schedule_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = schedule_stack.api(port=schedule_stack.port_a)
    api_b = schedule_stack.api(port=schedule_stack.port_b)
    key = uniq("sched")
    schedule_name = uniq("friendly")

    # Base tool + typed crontab THROUGH replica A: the door dispatches the
    # e2e_record_schedule_task vehicle with the cadence, honoring the explicit name.
    await api_a.post(
        "/api/schedules",
        json={
            "tool_name": "e2e_record",
            "tool_kwargs": {"key": key, "value": "tick"},
            "cadence": {"kind": "crontab", "minute": "*", "hour": "*"},
            "schedule_name": schedule_name,
        },
    )

    # The schedule is registered and appears in the cross-worker listing read from
    # replica B — the state lives in Redis, not the worker that created it.
    async def listed() -> dict[str, Any] | None:
        listing = await api_b.get("/api/schedules")
        return next((row for row in listing if row.get("name") == schedule_name), None)

    row = await wait_for_async(
        listed, deadline=40.0, message=f"crontab schedule {schedule_name!r} never appeared in the listing"
    )
    assert row["tool"] == "e2e_record", row
    assert row["cadence"] == {
        "kind": "crontab",
        "minute": "*",
        "hour": "*",
        "day_of_month": "*",
        "month_of_year": "*",
        "day_of_week": "*",
    }, row
    assert row["enabled"] is True, row
    assert "kwargs" not in row, row

    # Unschedule THROUGH replica B — the schedule B never created.
    await api_b.delete(f"/api/schedules/{schedule_name}")

    async def gone() -> bool:
        listing = await api_b.get("/api/schedules")
        return not any(row.get("name") == schedule_name for row in listing)

    await wait_for_async(
        gone, deadline=30.0, message=f"crontab schedule {schedule_name!r} still listed after unschedule"
    )
