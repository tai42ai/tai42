"""``schedule_task`` end-to-end on every backend leg, the sharpest
backend divergence: arq schedules on a self-rescheduling in-worker queue job,
celery on a RedBeat ``beat`` process, rq on the ``rqscheduler`` daemon. The boot
engine spawns whichever extra process the backend variant declares
(``extra_backend_processes``), so this one spec proves scheduling fires on all
three through the SAME public HTTP surface.

Cross-worker (the suite's thesis): a schedule registered through replica A fires,
is observed on the shared probe-record channel, and is unscheduled through replica
B — the schedule state lives in Redis, not in the worker that created it.

Scheduling + reload coexistence (the SUPPORTED topology): this drives a fleet reload
WHILE the schedule is live — proving a scheduled-task-bearing backend worker keeps
firing the schedule and still serves dispatched work after a config reload restarts
its worker pool. The UNSUPPORTED extreme (a schedule branch under the full preset
reload churn on celery) is pinned separately by
``tests/scheduling/test_schedule_reload_limitation.py``."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs(
    "kind:backend",
    "probe-tools",
    "mutable",
    "store:redis",
    "topology:replicas",
    "setting:extension:schedule_task",
    "setting:extension:sync_task",
    "setting:backend-scheduler-process",
)

# A whole-second interval: rq-scheduler re-arms on integer seconds, so a
# fractional period cannot be represented there.
_INTERVAL_SECONDS = 2


async def _firing_count(stack: TaiStack, key: str) -> int:
    """How many times the scheduled ``e2e_record`` has fired (one probe record
    per firing on the shared channel)."""
    return len(stack.records(key))


async def _wait_quiescent(stack: TaiStack, key: str, *, quiet_for: float, deadline: float) -> int:
    """Block until the firing count holds steady for ``quiet_for`` seconds, then
    return it — the bounded quiet window that proves an unschedule stopped the
    schedule. Polling a monotonic clock, never a fixed sleep."""
    state = {"count": -1, "since": 0.0}

    async def settled() -> bool:
        now = time.monotonic()
        count = await _firing_count(stack, key)
        if count != state["count"]:
            state["count"], state["since"] = count, now
            return False
        return now - state["since"] >= quiet_for

    await wait_for_async(
        settled, deadline=deadline, interval=0.3, message=f"schedule {key!r} never went quiet after unschedule"
    )
    return int(state["count"])


# The MCP reload call may run to the harness read bound (210 s: the 90 s default apply window +
# the 120 s boot bound), so the per-test bound sits above it: the stack boot inside the first
# test's timer (120) + the reload call's read bound (210) + a session re-init (15) + the two
# firing waits (2 x 40) + the dispatch's reload-gate wait (10) + the quiescence wait (30).
@pytest.mark.timeout(480)
async def test_schedule_fires_across_reload_then_unschedules_cross_worker(
    schedule_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api_a = schedule_stack.api(port=schedule_stack.port_a)
    api_b = schedule_stack.api(port=schedule_stack.port_b)
    key = uniq("sched")
    schedule_name = uniq("schedule")

    # Register the recurring schedule THROUGH replica A: every firing runs
    # e2e_record(key, "tick") in the backend worker, appending one probe record.
    await api_a.post(
        "/api/schedules",
        json={
            "tool_name": "e2e_record",
            "tool_kwargs": {"key": key, "value": "tick"},
            "cadence": {"kind": "interval", "every": _INTERVAL_SECONDS},
            "schedule_name": schedule_name,
        },
    )

    # At least TWO firings proves periodicity (not a one-shot). Generous deadline:
    # celery's RedBeat and rq's rqscheduler each take a beat loop to pick up a
    # freshly-created schedule.
    async def fired_twice() -> bool:
        return await _firing_count(schedule_stack, key) >= 2

    await wait_for_async(
        fired_twice, deadline=40.0, message=f"schedule {schedule_name!r} never fired twice on the probe channel"
    )

    # A fleet reload WHILE the schedule is live restarts the backend worker's pool
    # (on celery, a prefork pool-refork). A scheduled-task-bearing worker must
    # survive it: the schedule keeps firing and dispatched work still runs.
    before_reload = await _firing_count(schedule_stack, key)
    async with schedule_stack.mcp(port=schedule_stack.port_a) as mcp:
        # Prime the SDK's per-session tool-output-schema cache: reload_config retires this
        # session (the swapped-in epoch serves a new session-id space), so its
        # post-call output validation would otherwise issue a follow-up tools/list on the
        # orphaned session -> "Session terminated". A primed cache skips that follow-up.
        await mcp.list_tools()
        reload_result = await mcp.call_tool("reload_config", {})
    assert not reload_result.is_error, reload_result

    # The schedule keeps firing through the pool restart (RedBeat state is in
    # Redis; the reloaded worker re-forks and resumes serving the scheduled task).
    async def fired_after_reload() -> bool:
        return await _firing_count(schedule_stack, key) > before_reload

    await wait_for_async(
        fired_after_reload, deadline=40.0, message=f"schedule {schedule_name!r} stopped firing after a fleet reload"
    )

    # And the post-reload worker pool still serves dispatched backend work — the
    # exact execution path a wedged pool would hang. The reload restarts EVERY
    # replica's pool, so a request landing before a replica leaves its boot-time
    # reload gate is answered with a retriable ``503 reloading`` — ``fired_after_reload``
    # only proves the schedule-bearing worker resumed firing, not that this replica's
    # API surface left the gate. Poll past it on the sanctioned bounded deadline
    # (``retry_on_reloading``), exactly as every other post-reload call in the suite.
    async with schedule_stack.mcp(port=schedule_stack.port_a) as mcp:
        dispatched = await mcp.call_tool(
            "run_tool_sync_task",
            {"tool_name": "e2e_echo", "arguments": {"payload": "after-reload"}},
            retry_on_reloading=True,
        )
    assert not dispatched.is_error, dispatched

    # The schedule appears in the cross-worker listing read from replica B — which may
    # still be finishing its own reload, so ride past the gate rather than race it.
    listing = await api_b.get("/api/schedules", retry_on_reloading=True)
    (row,) = [row for row in listing if row.get("name") == schedule_name]
    # A typed ``ScheduleListRow`` on every backend: the fired tool and the cadence.
    assert row["tool"] == "e2e_record", row
    assert row["cadence"] == {"kind": "interval", "every": float(_INTERVAL_SECONDS), "relative": False}, row

    # Unschedule THROUGH replica B — the schedule B never created, proving the
    # state is shared, not worker-local.
    await api_b.delete(f"/api/schedules/{schedule_name}", retry_on_reloading=True)

    # Firing stops: the count holds steady through a quiet window (one already
    # in-flight firing may still land, so assert quiescence, not an exact count).
    quiet_count = await _wait_quiescent(schedule_stack, key, quiet_for=_INTERVAL_SECONDS * 2.0, deadline=30.0)
    assert quiet_count >= 2

    # And it is gone from the listing.
    listing_after = await api_b.get("/api/schedules", retry_on_reloading=True)
    assert not any(row.get("name") == schedule_name for row in listing_after), listing_after
