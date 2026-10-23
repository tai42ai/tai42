"""The shutdown stop cancels an attempt blocked inside the work its hook started, past the grace.

The job is freed by the interrupted-attempt rule: a start of a tool that declares crash-resume is
released and its start replayed alone; every other acted run is held as failed.
"""

from __future__ import annotations

import asyncio

from tai42_contract.template import TemplatedText
from tai42_contract.tools import TOOL_EXTRA_IDEMPOTENCY_KEY

from tai42_skeleton.hooks.managers import base_hooks_manager
from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs.sweep import stop_hook_runs_sweep, sweep_tick

from .conftest import blocked, get_run, held_run, hook, real_hook, settle_loop

_GRACE = 0.05


def _count_evaluations(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = base_hooks_manager.evaluate_door_contract

    async def _evaluate(contract, payload, parked):
        seen.append(contract.name)
        return await real(contract, payload, parked)

    monkeypatch.setattr(base_hooks_manager, "evaluate_door_contract", _evaluate)
    return seen


async def _serve_again(hook_runs) -> None:
    """Start serving again as a fresh process lifespan would, and run one sweep tick."""
    runtime.set_stopping(False)
    await sweep_tick()
    await hook_runs.drain()


async def test_an_unflagged_tool_blocked_past_the_grace_fails(backend, world, hook_runs, clock, notices) -> None:
    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=_GRACE))
    await manager.register(hook("a"))
    gate, started = asyncio.Event(), asyncio.Event()
    world.tools.behave["probe"] = blocked(gate, started)

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await started.wait()
    await stop_hook_runs_sweep()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted, run.last_error) == (
        "failed",
        "start",
        "interrupted after the hook acted (start)",
    )
    assert [n for n in notices if run_id in n]
    clock.advance(manager.settings.run_lease_seconds + 1)
    await _serve_again(hook_runs)
    assert len(world.tools.calls) == 1
    assert (await held_run(manager, run_id)).status == "failed"


async def test_a_flagged_tool_blocked_past_the_grace_is_released_and_replayed(
    backend, world, hook_runs, clock, monkeypatch
) -> None:
    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=_GRACE))
    await manager.register(hook("a"))
    world.tools.flagged = {"probe"}
    gate, started = asyncio.Event(), asyncio.Event()
    world.tools.behave["probe"] = blocked(gate, started)
    evaluations = _count_evaluations(monkeypatch)

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await started.wait()
    await stop_hook_runs_sweep()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.attempts, run.acted, run.next_attempt_at, run.claimed_by) == (
        "pending",
        1,
        "start",
        clock.t,
        None,
    )
    assert run.start is not None
    assert run.start.tool == "probe"
    assert evaluations == ["a"]

    gate.set()
    await _serve_again(hook_runs)

    assert await get_run(manager, run_id) is None
    first, replay = world.tools.calls
    assert replay.input == first.input
    assert replay.extras[TOOL_EXTRA_IDEMPOTENCY_KEY] == first.extras[TOOL_EXTRA_IDEMPOTENCY_KEY] == run_id
    assert evaluations == ["a"]
    assert len(world.interactions.parked_reads) == 1


async def test_an_attempt_blocked_inside_a_resumed_continuation_fails(
    backend, real_world, hook_runs, clock, monkeypatch, notices
) -> None:
    from tai42_skeleton.interactions import visit as visit_module

    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=_GRACE))
    await manager.register(real_hook("a", resume_expr=TemplatedText(content='{id: "i1", payload: {value: 1}}')))
    await real_world.park("i1")
    entered = asyncio.Event()

    async def _drive_forever(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(visit_module, "drive_and_deliver", _drive_forever)

    [run_id] = (await manager.on_event("t", {"who": "pA"})).run_ids
    await entered.wait()
    await stop_hook_runs_sweep()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted, run.acted_ids, run.last_error) == (
        "failed",
        "resume",
        ("i1",),
        "interrupted after the hook acted (resume)",
    )
    assert await real_world.status("i1") == "answered"
    assert [n for n in notices if run_id in n]
    await settle_loop()
    assert (await held_run(manager, run_id)).status == "failed"
