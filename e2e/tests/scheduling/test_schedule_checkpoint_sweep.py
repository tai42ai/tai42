"""The conversation-checkpoint sweep is natively schedulable.

Scheduling any tool means branching it with the backend's ``schedule_task`` extension; a
projected operation-tool (``sweep_checkpoints``) has no manifest extension hook of its own, so
it schedules through ``run_tool_schedule_task`` — the scheduling analog of the universal
``run_tool_sync_task`` door — which the schedule stack mints. This is the mechanism a
deployment uses for native recurrence; external cron of ``tai checkpoints sweep`` is the
busless alternative. Firing mechanics are proven per-backend by ``test_schedule_task``; the
sweep's own execution semantics by the operation tests + the bridge API leg, so this asserts
the register/list/delete depth the schedulability claim needs.
"""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack


async def test_checkpoint_sweep_is_schedulable(schedule_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = schedule_stack.api(port=schedule_stack.port_a)
    schedule_name = uniq("sweep-schedule")
    # A long cadence so the schedule is registered, listed, and removed without firing.
    await api.post(
        "/api/schedules",
        json={
            "tool_name": "run_tool_schedule_task",
            "tool_kwargs": {"tool_name": "sweep_checkpoints", "arguments": {}},
            "schedule_kwargs": {"backend_schedule_name": schedule_name, "backend_schedule": 3600},
        },
    )
    try:
        listing = await api.get("/api/schedules")
        assert any(row.get("name") == schedule_name for row in listing), listing
    finally:
        await api.delete(f"/api/schedules/{schedule_name}")
        listing_after = await api.get("/api/schedules")
        assert not any(row.get("name") == schedule_name for row in listing_after), listing_after
