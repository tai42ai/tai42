"""The hook-run runtime is per process, not per manager: a config apply that drops the manager keeps it.

The attempts begun before an apply stay in the one attempt registry, the shutdown stop reaches
them, they finish through the fresh manager, and ``HOOKS_MAX_WORKERS`` bounds them together with
the attempts begun after it. The tool-run store is off, so the grace alone governs a tool.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from tai42_contract.template import TemplatedText
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.hooks import cache
from tai42_skeleton.hooks.managers.redis_hooks_manager import RedisHooksManager
from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs.sweep import stop_hook_runs_sweep, sweep_tick

from .conftest import blocked, get_run, held_run, hook, settle_loop

_REAL_MANAGER = runtime.manager


@pytest.fixture
def current_manager(monkeypatch, hook_runs):
    """Every hook-run step reads the process's real hooks manager, which a settings reset drops."""
    monkeypatch.setattr(runtime, "manager", _REAL_MANAGER)
    cache._reset_hooks_manager()
    yield cache.get_hooks_manager
    cache._reset_hooks_manager()


@pytest.fixture
def redis_hooks(monkeypatch, backend, world, current_manager):
    """The hooks manager on Redis (the backend's fake), selected by ``HOOKS_REDIS_URL``."""
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/0")
    cache._reset_hooks_manager()
    return backend


async def _retire_epoch(monkeypatch) -> None:
    from tai42_skeleton.app import epoch_retire
    from tai42_skeleton.app.epoch import Epoch

    async def _no_pool_drain(epoch, deadline):
        return None

    monkeypatch.setattr(epoch_retire, "drain_epoch", _no_pool_drain)
    monkeypatch.setattr(epoch_retire, "arm_retired_generation_certification", lambda old, retired: None)
    await epoch_retire.retire_epoch(Epoch(number=11), 11, 1.0)


async def _apply(how: str, monkeypatch) -> None:
    if how == "reset":
        reset_all_settings()
    else:
        await _retire_epoch(monkeypatch)


# -- an apply, then the shutdown --------------------------------------------------------


@pytest.mark.parametrize("backend", ["redis"], indirect=True)
@pytest.mark.parametrize("how", ["reset", "retire"])
async def test_the_attempts_begun_before_an_apply_are_reached_by_the_stop(
    how, redis_hooks, world, current_manager, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("HOOKS_RUN_SHUTDOWN_GRACE_SECONDS", "0.5")
    cache._reset_hooks_manager()
    before = current_manager()
    await before.register(hook("a", topic="ta", tool="slow"))
    await before.register(hook("b", topic="tb"))
    gate_a, started_a = asyncio.Event(), asyncio.Event()
    world.tools.behave["slow"] = blocked(gate_a, started_a)

    [a_id] = (await before.on_event("ta", {"n": 1})).run_ids
    await started_a.wait()
    world.interactions.parked_gate = asyncio.Event()
    world.interactions.parked_started.clear()
    [b_id] = (await before.on_event("tb", {"n": 2})).run_ids
    await world.interactions.parked_started.wait()
    a_run = await get_run(before, a_id)
    assert a_run is not None
    assert a_run.acted == "start"

    await _apply(how, monkeypatch)

    after = current_manager()
    assert after is not before
    assert {a_id, b_id} <= set(runtime._ATTEMPTS)

    caplog.set_level(logging.WARNING)
    asyncio.get_running_loop().call_later(0.05, gate_a.set)
    await stop_hook_runs_sweep()

    assert await get_run(after, a_id) is None
    assert not [key for key in await redis_hooks.redis.keys("*") if a_id in key]
    b_run = await get_run(after, b_id)
    assert b_run is not None
    assert (b_run.status, b_run.attempts, b_run.acted, b_run.claimed_by) == ("pending", 1, "", None)
    assert b_run.next_attempt_at <= runtime.now()
    assert [r for r in caplog.records if r.levelno == logging.WARNING and b_id in r.getMessage()]

    assert runtime._ATTEMPTS == {}
    [c_id] = (await after.on_event("tb", {"n": 3})).run_ids
    assert runtime.attempt_count() == 0
    c_run = await get_run(after, c_id)
    assert c_run is not None
    assert (c_run.status, c_run.claimed_by) == ("pending", None)


# -- the bound across an apply ----------------------------------------------------------


class _Gauge:
    """A tool behaviour per call that blocks on its own gate and tracks the concurrent peak."""

    def __init__(self) -> None:
        self.running = 0
        self.peak = 0
        self.peaks_after_old: list[int] = []
        self.old_done = False
        self.gates: list[asyncio.Event] = []
        self.started: list[asyncio.Event] = []

    def arm(self, count: int) -> None:
        self.gates = [asyncio.Event() for _ in range(count)]
        self.started = [asyncio.Event() for _ in range(count)]

    async def __call__(self, call):
        index = call.input["n"]
        self.running += 1
        self.peak = max(self.peak, self.running)
        if self.old_done:
            self.peaks_after_old.append(self.running)
        self.started[index].set()
        try:
            await self.gates[index].wait()
        finally:
            self.running -= 1
        return {"ok": True}


def _counted_hook():
    return hook("a", start_expr=TemplatedText(content="{n: .n}"))


async def _accept(manager, topic: str, n: int) -> str:
    [run_id] = (await manager.on_event(topic, {"n": n})).run_ids
    return run_id


@pytest.mark.parametrize("backend", ["redis"], indirect=True)
async def test_a_lowered_limit_bounds_the_old_and_the_new_attempts_together(
    redis_hooks, world, current_manager, monkeypatch
) -> None:
    monkeypatch.setenv("HOOKS_MAX_WORKERS", "2")
    cache._reset_hooks_manager()
    gauge = _Gauge()
    gauge.arm(3)
    world.tools.behave["probe"] = gauge
    manager = current_manager()
    await manager.register(_counted_hook())

    await _accept(manager, "t", 0)
    await _accept(manager, "t", 1)
    await gauge.started[0].wait()
    await gauge.started[1].wait()

    monkeypatch.setenv("HOOKS_MAX_WORKERS", "1")
    reset_all_settings()
    await _accept(current_manager(), "t", 2)
    await settle_loop()
    assert not gauge.started[2].is_set()
    assert len(runtime._WAITERS) == 1

    gauge.gates[0].set()
    await settle_loop()
    assert not gauge.started[2].is_set()

    gauge.old_done = True
    gauge.gates[1].set()
    await gauge.started[2].wait()
    gauge.gates[2].set()
    await runtime.drain_hook_runs()

    assert gauge.peak == 2
    assert gauge.peaks_after_old == [1]
    assert runtime._SLOTS_IN_USE == 0


@pytest.mark.parametrize("backend", ["redis"], indirect=True)
async def test_a_raised_limit_admits_a_new_attempt_at_once_and_the_waiting_one_at_the_next_release(
    redis_hooks, world, current_manager, monkeypatch
) -> None:
    monkeypatch.setenv("HOOKS_MAX_WORKERS", "1")
    cache._reset_hooks_manager()
    gauge = _Gauge()
    gauge.arm(3)
    world.tools.behave["probe"] = gauge
    manager = current_manager()
    await manager.register(_counted_hook())

    await _accept(manager, "t", 0)
    await gauge.started[0].wait()
    await _accept(manager, "t", 1)
    await settle_loop()
    assert len(runtime._WAITERS) == 1

    monkeypatch.setenv("HOOKS_MAX_WORKERS", "3")
    reset_all_settings()
    await _accept(current_manager(), "t", 2)
    await gauge.started[2].wait()
    assert not gauge.started[1].is_set()

    gauge.gates[0].set()
    await gauge.started[1].wait()
    gauge.gates[1].set()
    gauge.gates[2].set()
    await runtime.drain_hook_runs()
    assert runtime._SLOTS_IN_USE == 0


@pytest.mark.parametrize("backend", ["redis"], indirect=True)
async def test_a_cancelled_waiter_leaves_the_line_and_takes_no_slot(
    redis_hooks, world, current_manager, monkeypatch
) -> None:
    monkeypatch.setenv("HOOKS_MAX_WORKERS", "1")
    cache._reset_hooks_manager()
    gauge = _Gauge()
    gauge.arm(2)
    world.tools.behave["probe"] = gauge
    manager = current_manager()
    await manager.register(_counted_hook())

    await _accept(manager, "t", 0)
    await gauge.started[0].wait()
    waiting = await _accept(manager, "t", 1)
    await settle_loop()
    assert len(runtime._WAITERS) == 1

    task = runtime._ATTEMPTS[waiting]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(runtime._WAITERS) == 0
    assert runtime._SLOTS_IN_USE == 1
    assert not gauge.started[1].is_set()
    gauge.gates[0].set()
    await runtime.drain_hook_runs()
    assert runtime._SLOTS_IN_USE == 0


# -- the sweep counts every generation --------------------------------------------------


@pytest.mark.parametrize("backend", ["redis"], indirect=True)
async def test_the_sweep_counts_the_attempts_of_every_manager_generation(
    redis_hooks, world, hook_runs, current_manager, monkeypatch
) -> None:
    monkeypatch.setenv("HOOKS_MAX_WORKERS", "2")
    cache._reset_hooks_manager()
    gauge = _Gauge()
    gauge.arm(3)
    world.tools.behave["probe"] = gauge
    manager = current_manager()
    await manager.register(_counted_hook())
    await _accept(manager, "t", 0)
    await _accept(manager, "t", 1)
    await gauge.started[0].wait()
    await gauge.started[1].wait()
    hook_runs.off_serving_loop()
    due = await _accept(manager, "t", 2)
    hook_runs.back_on_serving_loop()

    reset_all_settings()
    limits: list[int] = []
    claim = RedisHooksManager._claim_due_runs

    async def _spy(self, now, limit, *args, **kwargs):
        limits.append(limit)
        return await claim(self, now, limit, *args, **kwargs)

    monkeypatch.setattr(RedisHooksManager, "_claim_due_runs", _spy)
    await sweep_tick()

    assert limits in ([], [0])
    assert (await held_run(current_manager(), due)).status == "pending"
    for gate in gauge.gates[:2]:
        gate.set()
    await runtime.drain_hook_runs()


# -- the in-memory reset is loud --------------------------------------------------------


async def test_an_in_memory_reset_under_a_running_attempt_is_loud(world, current_manager, caplog) -> None:
    manager = current_manager()
    await manager.register(hook("a"))
    gate, started = asyncio.Event(), asyncio.Event()
    world.tools.behave["probe"] = blocked(gate, started)
    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await started.wait()

    caplog.set_level(logging.WARNING)
    reset_all_settings()
    [reset_warning] = [r for r in caplog.records if "discarding the in-memory hooks manager" in r.getMessage()]
    assert "1 running" in reset_warning.getMessage()

    gate.set()
    await runtime.drain_hook_runs()
    assert [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and run_id in r.getMessage() and "no longer holds its lease" in r.getMessage()
    ]
