"""An accept off the serving loop keeps unclaimed jobs due now and starts nothing; the sweep runs them."""

from __future__ import annotations

from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs.sweep import sweep_tick

from .conftest import all_runs, hook


async def test_an_offloop_accept_keeps_due_jobs_the_sweep_runs(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    await manager.register(hook("b"))
    hook_runs.off_serving_loop()

    result = await manager.on_event("t", {"n": 1})

    kept = await all_runs(manager)
    assert sorted(run.id for run in kept) == sorted(result.run_ids)
    for run in kept:
        assert (run.status, run.attempts, run.claimed_by, run.lease_until) == ("pending", 0, None, None)
        assert run.next_attempt_at == clock.t
    assert runtime.live_attempt_ids() == frozenset()
    assert world.tools.calls == []

    hook_runs.back_on_serving_loop()
    await sweep_tick()
    await hook_runs.drain()

    assert await all_runs(manager) == []
    assert len(world.tools.calls) == 2
