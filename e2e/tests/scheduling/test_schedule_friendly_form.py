"""The friendly create form end-to-end: a BASE tool plus a ``cron`` string, no
expert ``backend_schedule`` keys from the caller.

``test_schedule_task`` proves the expert shape (the caller names
``e2e_record_schedule_task`` and passes ``backend_schedule``). This proves the
documented friendly shape on the same public HTTP surface: the create door resolves
the ``e2e_record_schedule_task`` vehicle itself and translates ``cron`` onto its
``backend_schedule``, so a real backend schedule is registered and readable in the
cross-worker listing. A crontab cadence fires on minute boundaries, so this asserts
registration and unschedule (fast and deterministic) rather than firing count —
periodicity is already proven by the interval spec in ``test_schedule_task``."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack


async def test_friendly_cron_form_registers_and_unschedules_cross_worker(
    schedule_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = schedule_stack.api(port=schedule_stack.port_a)
    api_b = schedule_stack.api(port=schedule_stack.port_b)
    key = uniq("sched")
    schedule_name = uniq("friendly")

    # Base tool + friendly cron THROUGH replica A: the door translates cron onto the
    # e2e_record_schedule_task branch's backend_schedule, honoring the explicit name.
    await api_a.post(
        "/api/schedules",
        json={
            "tool_name": "e2e_record",
            "tool_kwargs": {"key": key, "value": "tick"},
            "schedule_kwargs": {"cron": "* * * * *", "backend_schedule_name": schedule_name},
        },
    )

    # The translated schedule is registered and appears in the cross-worker listing read
    # from replica B — the state lives in Redis, not the worker that created it.
    async def listed() -> bool:
        listing = await api_b.get("/api/schedules")
        return any(row.get("name") == schedule_name for row in listing)

    await wait_for_async(
        listed, deadline=40.0, message=f"friendly schedule {schedule_name!r} never appeared in the listing"
    )

    # Unschedule THROUGH replica B — the schedule B never created.
    await api_b.delete(f"/api/schedules/{schedule_name}")

    async def gone() -> bool:
        listing = await api_b.get("/api/schedules")
        return not any(row.get("name") == schedule_name for row in listing)

    await wait_for_async(
        gone, deadline=30.0, message=f"friendly schedule {schedule_name!r} still listed after unschedule"
    )
