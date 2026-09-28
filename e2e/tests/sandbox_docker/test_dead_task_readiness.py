"""The perpetual-task readiness marker on a live SUT that runs all five perpetual tasks.

``/ready`` names a perpetual background task that has died and returns 503, and the
process requests its own graceful exit so a ``restart: unless-stopped`` supervisor
reboots it from a clean baseline. This leg drives the marker read against a running
SUT that spawns every perpetual task: the sandbox stack registers a sandbox provider,
so the sandbox reaper joins the worker-bus subscription, the failed-MCP re-probe loop,
the interactions expiry reaper and the inbound-media retention reaper. With every task
alive the marker stays unset, so the readiness probe readies with no
``perpetual_task:<name>`` check — the marker read must never false-positive on a
healthy fleet.
"""

from __future__ import annotations

from tai42_e2e import readiness
from tai42_e2e.stack import TaiStack


async def test_ready_names_no_dead_task_while_perpetual_tasks_run(sandbox_stack: TaiStack) -> None:
    """A healthy SUT with all five perpetual tasks running readies (200) and names no
    dead perpetual task, proving ``/ready`` reads the dead-task marker without
    false-positiving while every task is alive."""
    checks = readiness.assert_no_dead_perpetual_task(sandbox_stack)
    assert checks is not None
