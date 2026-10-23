"""The shutdown stop cancels an attempt still running past the grace before its hook acted, and releases its job."""

from __future__ import annotations

import asyncio
import logging

from tai42_skeleton.hooks.runs.sweep import stop_hook_runs_sweep

from .conftest import get_run, hook


async def test_an_attempt_blocked_before_acting_is_released(backend, world, hook_runs, clock, caplog) -> None:
    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=0.05))
    await manager.register(hook("a"))
    world.interactions.parked_gate = asyncio.Event()
    caplog.set_level(logging.WARNING)

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await world.interactions.parked_started.wait()
    await stop_hook_runs_sweep()

    run = await get_run(manager, run_id)
    assert run is not None
    assert run.status == "pending"
    assert run.next_attempt_at == clock.t
    assert run.attempts == 1
    assert run.acted == ""
    assert run.claimed_by is None
    assert world.tools.calls == []
    assert [r for r in caplog.records if r.levelno == logging.WARNING and run_id in r.getMessage()]


async def test_an_attempt_waiting_for_a_slot_is_released(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager(max_workers=1, run_shutdown_grace_seconds=0.05))
    await manager.register(hook("a"))
    await manager.register(hook("b"))
    world.interactions.parked_gate = asyncio.Event()

    run_ids = (await manager.on_event("t", {"n": 1})).run_ids
    await world.interactions.parked_started.wait()
    await stop_hook_runs_sweep()

    for run_id in run_ids:
        run = await get_run(manager, run_id)
        assert run is not None
        assert (run.status, run.attempts, run.acted, run.next_attempt_at) == ("pending", 1, "", clock.t)
    # Only one attempt ever held the slot and read ``$parked``; the other waited for it.
    assert len(world.interactions.parked_reads) == 1


async def test_an_attempt_interrupted_on_its_last_attempt_fails(backend, world, hook_runs, clock, notices) -> None:
    manager = hook_runs.use(backend.manager(run_max_attempts=1, run_shutdown_grace_seconds=0.05))
    await manager.register(hook("a"))
    world.interactions.parked_gate = asyncio.Event()

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await world.interactions.parked_started.wait()
    await stop_hook_runs_sweep()

    run = await get_run(manager, run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.acted == ""
    assert run.last_error == "interrupted on attempt 1 of 1 and no attempts remain (acted=)"
    assert [n for n in notices if run_id in n]
