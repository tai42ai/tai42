"""A job whose holder died is reclaimed by another process once its lease lapses; a live lease never is."""

from __future__ import annotations

import asyncio

from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs.sweep import sweep_tick

from .conftest import all_runs, as_process, blocked, get_run, hook


def dead_holder(monkeypatch) -> None:
    """Accept as a process that dies at once: its jobs are kept claimed, and no attempt ever runs."""
    as_process(monkeypatch, "dead-host:1:aaaa")
    monkeypatch.setattr(runtime, "spawn_attempt", lambda job: None)


async def test_a_lapsed_lease_is_reclaimed_by_another_process(backend, world, hook_runs, clock, monkeypatch) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    spawn = runtime.spawn_attempt
    dead_holder(monkeypatch)
    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    kept = await get_run(manager, run_id)
    assert kept is not None
    assert (kept.status, kept.claimed_by, kept.attempts) == ("running", "dead-host:1:aaaa", 1)

    peer = hook_runs.use(backend.peer(manager))
    as_process(monkeypatch, "live-host:2:bbbb")
    monkeypatch.setattr(runtime, "spawn_attempt", spawn)
    seen: list = []
    original_claim = peer._claim_due_runs

    async def _claim(*args):
        claimed = await original_claim(*args)
        seen.extend(claimed)
        return claimed

    monkeypatch.setattr(peer, "_claim_due_runs", _claim)

    # Still within the lease: the other process claims nothing.
    clock.advance(manager.settings.run_lease_seconds - 1)
    await sweep_tick()
    assert seen == []

    clock.advance(2)
    await sweep_tick()
    await hook_runs.drain()

    [reclaimed] = seen
    assert reclaimed.id == run_id
    assert reclaimed.reclaimed is True
    assert reclaimed.attempts == 2
    assert reclaimed.claimed_by == "live-host:2:bbbb"
    assert len(world.tools.calls) == 1
    assert await all_runs(peer) == []


async def test_a_live_lease_is_never_claimed_by_another_process(backend, world, hook_runs, clock, monkeypatch) -> None:
    lease = 0.3
    manager = hook_runs.use(backend.manager(run_lease_seconds=lease))
    await manager.register(hook("a"))
    gate, started = asyncio.Event(), asyncio.Event()
    world.tools.behave["probe"] = blocked(gate, started)
    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await started.wait()

    # The clock passes the first lease; the holder's heartbeat (every lease / 3) renews it.
    clock.advance(lease * 2)
    await asyncio.sleep(lease / 3 + 0.1)
    held = await get_run(manager, run_id)
    assert held is not None
    assert held.lease_until is not None
    assert held.lease_until > clock.t

    # Another process's sweep: it has no attempt of its own, and finds the lease live.
    peer = hook_runs.use(backend.peer(manager))
    holder = runtime.claimed_by()
    as_process(monkeypatch, "other-host:3:cccc")
    monkeypatch.setattr(runtime, "live_attempt_ids", lambda: frozenset())
    await sweep_tick()

    still = await get_run(peer, run_id)
    assert still is not None
    assert (still.status, still.claimed_by, still.attempts) == ("running", holder, 1)

    gate.set()
    await hook_runs.drain()
    assert len(world.tools.calls) == 1
    assert await all_runs(peer) == []
