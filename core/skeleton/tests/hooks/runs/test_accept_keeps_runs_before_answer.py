"""An accept keeps one run per hook on the topic before it answers; the runs happen afterwards."""

from __future__ import annotations

import asyncio

from .conftest import all_runs, blocked, get_run, hook


async def test_accept_answers_one_run_id_per_hook(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    await manager.register(hook("b"))
    await manager.register(hook("other", topic="elsewhere"))

    result = await manager.on_event("t", {"n": 1})
    await hook_runs.drain()

    assert result.status == "accepted"
    assert len(result.run_ids) == 2
    assert len(set(result.run_ids)) == 2
    assert sorted(call.name for call in world.tools.calls) == ["probe", "probe"]


async def test_runs_are_kept_before_any_tool_runs(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    gate = asyncio.Event()
    started = asyncio.Event()
    world.tools.behave["probe"] = blocked(gate, started)

    result = await manager.on_event("t", {"n": 1})

    # The accept has answered; the run is already in the store, claimed by this process, with its
    # payload, while its tool has not even begun.
    assert world.tools.calls == []
    [run_id] = result.run_ids
    kept = await get_run(manager, run_id)
    assert kept is not None
    assert kept.status == "running"
    assert kept.attempts == 1
    assert kept.payload == {"n": 1}
    assert kept.hook == "a"
    assert kept.topic == "t"

    await started.wait()
    gate.set()
    await hook_runs.drain()
    assert len(world.tools.calls) == 1
    # A run that succeeded is deleted.
    assert await all_runs(manager) == []


async def test_a_topic_with_no_hooks_keeps_nothing(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())

    result = await manager.on_event("t", {"n": 1})

    assert result.status == "accepted"
    assert result.run_ids == ()
    assert await all_runs(manager) == []
