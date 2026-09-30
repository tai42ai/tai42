"""A perpetual background task DYING on a live SUT flips ``/ready`` and self-recovers.

The healthy leg (``test_dead_task_readiness``) proves ``/ready`` names no dead task
while every perpetual task runs. This leg induces a genuine death in the running SUT and
asserts the whole recovery chain on reality: the ``e2e_kill_perpetual_task`` probe ends a
named run-until-cancelled lifespan task through the app's own ``_on_perpetual_task_done``
seam, so the dead-task marker is set, ``/ready`` names the task and returns 503, and the
process requests its own graceful exit. On the SUPERVISED recycle stack (the harness
respawn-on-exit supervisor stands in for compose ``restart: unless-stopped``) the
self-exited worker is respawned and rejoins the bus census at a bumped generation, and a
fresh process readies again with the marker unset.

Both perpetual-task-bearing processes are covered: the serve worker directly (it owns the
``/ready`` door, so the 503 truth is read there), and the backend runtime worker through
``run_tool_sync_task`` — it runs the probe INSIDE the backend, whose death routes through
the SAME ``_on_perpetual_task_done`` handler and the backend graceful-exit primitive.
The backend has no probed HTTP readiness door under the shipped compose, so its recovery
signal is the EXIT alone, observed as the supervised respawn.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import pytest

from tai42_e2e import readiness
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for

# The perpetual lifespan tasks the legs end. Both the interactions expiry reaper and the
# inbound-media retention reaper run in the serve and the backend process (spawned
# unconditionally in ``app_context``), so either target name exercises the shared
# done-callback on both. The serve leg parametrizes over the two to prove the seam names
# and recovers whichever perpetual task died; the backend leg drives the interactions reaper.
_TASK = "tai-interactions-expiry-reaper"
_MEDIA_TASK = "tai-media-retention-reaper"
_REASON = "RuntimeError"


def _lives(stack: TaiStack, kind: str) -> dict[str, int]:
    """The live ``{slot name: generation}`` of a worker kind on the bus census."""
    return {worker.name: worker.generation for worker in stack.census() if worker.kind == kind}


def _respawned(before: dict[str, int], now: dict[str, int]) -> bool:
    """A supervised self-exit respawns the same stable slot name at a bumped generation."""
    return any(name in now and now[name] > gen for name, gen in before.items())


def _ready_ok(stack: TaiStack) -> bool:
    """``/ready`` answers 200 — tolerating the connection drop / 503 while a respawn boots."""
    try:
        return readiness.ready_status(stack)[0] == 200
    except httpx.HTTPError:
        return False


async def _fire_self_exit(stack: TaiStack, name: str, arguments: dict[str, Any], timeout: float) -> None:
    """Call ``name`` to induce a graceful self-exit, bounded and tolerant of the drop.

    The targeted worker self-exits during the call, so the MCP session close can block on
    the dying connection; the call is bounded and its failure suppressed — the recovery is
    observed on the bus census, never on this call's return."""
    with contextlib.suppress(Exception):

        async def _call() -> None:
            async with stack.mcp() as mcp:
                await mcp.call_tool(name, arguments, retry_on_reloading=True)

        await asyncio.wait_for(_call(), timeout=timeout)


@pytest.mark.timeout(300)
@pytest.mark.needs("probe-tools", "mutable", "process", "store:redis", "topology:supervised")
@pytest.mark.parametrize("task_name", [_TASK, _MEDIA_TASK])
async def test_dead_serve_perpetual_task_readies_503_then_graceful_exit_respawns(
    recycle_stack: TaiStack,
    task_name: str,
) -> None:
    """A dead serve perpetual task → ``/ready`` 503 naming it → graceful exit → respawn.

    Parametrized over two of the unconditional ``app_context`` perpetual tasks (the
    interactions expiry reaper and the inbound-media retention reaper), so the shared
    ``_on_perpetual_task_done`` seam is proven to name and recover whichever one dies."""
    stack = recycle_stack
    deadline = stack.infra.settings.boot_timeout

    # Baseline: the serve worker readies with no dead-task check.
    readiness.assert_no_dead_perpetual_task(stack)

    # Mark the reaper dead WITHOUT the graceful exit, so the pre-exit /ready 503 truth is
    # readable over real HTTP on a process that stays up.
    async with stack.mcp() as mcp:
        marked = await mcp.call_tool(
            "e2e_kill_perpetual_task", {"name": task_name, "request_exit": False}, retry_on_reloading=True
        )
    assert marked.data["name"] == task_name, marked.data
    readiness.assert_ready_names_dead_perpetual_task(stack, task_name, _REASON)

    # Now the full death through the app's own done-callback: marker + graceful exit.
    before = _lives(stack, "serve")
    assert before, "no serve life on the bus before the injected death"
    await _fire_self_exit(stack, "e2e_kill_perpetual_task", {"name": task_name, "request_exit": True}, deadline)

    # The graceful exit is observed as the supervised respawn: the serve slot rejoins the
    # census at a bumped generation, and the fresh process readies with the marker unset.
    wait_for(
        lambda: _respawned(before, _lives(stack, "serve")) or None,
        deadline=deadline * 6,
        message=f"the self-exited serve worker never respawned (before {before})",
    )
    wait_for(
        lambda: _ready_ok(stack) or None,
        deadline=deadline * 2,
        message="the respawned serve worker never readied",
    )


@pytest.mark.timeout(300)
@pytest.mark.needs(
    "kind:backend",
    "probe-tools",
    "mutable",
    "process",
    "store:redis",
    "topology:supervised",
    "setting:extension:sync_task",
)
async def test_dead_backend_perpetual_task_graceful_exits_and_respawns(
    recycle_stack: TaiStack,
) -> None:
    """A dead backend perpetual task → the backend runtime graceful-exits and respawns.

    Driven through ``run_tool_sync_task`` so the probe runs inside the backend worker; its
    death routes through the same ``_on_perpetual_task_done`` handler and the backend
    graceful-exit primitive. The backend has no HTTP readiness door, so the recovery is
    observed as the supervised respawn (its bus life rejoins at a bumped generation)."""
    stack = recycle_stack
    deadline = stack.infra.settings.boot_timeout

    before = _lives(stack, "backend")
    assert before, "no backend life on the bus before the injected death"

    await _fire_self_exit(
        stack,
        "run_tool_sync_task",
        {"tool_name": "e2e_kill_perpetual_task", "arguments": {"name": _TASK, "request_exit": True}},
        deadline * 3,
    )

    wait_for(
        lambda: _respawned(before, _lives(stack, "backend")) or None,
        deadline=deadline * 6,
        message=f"the self-exited backend runtime never respawned (before {before})",
    )
