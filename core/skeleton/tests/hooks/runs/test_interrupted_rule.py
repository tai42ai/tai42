"""The interrupted-attempt rule: what a job freed with no verdict does next, and its bound on replays.

A job whose hook has not acted runs again; a start of a tool that declares crash-resume is replayed
alone; every other acted job fails, and so does either of the first two once the attempts are used
up. The lease reclaim, the drain interruption and the shutdown stop all decide by this one rule.
"""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.hooks.runs import attempt as attempt_module
from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs.attempt import interrupted_disposition
from tai42_skeleton.hooks.runs.models import HookRun, StartSpec
from tai42_skeleton.hooks.runs.sweep import stop_hook_runs_sweep, sweep_tick

from .conftest import START, as_process, blocked, get_run, held_run, hook

_START = StartSpec(tool="probe", input={"n": 1}, extras={}, state_context=None, state_binding=None)


def _job(acted, *, start=None, attempts=1) -> HookRun:
    return HookRun(
        id="r-1",
        topic="t",
        hook="a",
        payload={},
        status="running",
        attempts=attempts,
        next_attempt_at=START,
        created_at=START,
        claimed_by="me",
        acted=acted,
        start=start,
        acted_ids=("i1",) if acted in ("cancel", "resume", "take") else (),
        claim_intent=None,
    )


@pytest.mark.parametrize(
    ("acted", "start", "flagged", "expected"),
    [
        ("", None, False, "rerun"),
        ("start", _START, True, "replay_start"),
        ("start", _START, False, "fail"),
        ("cancel", None, False, "fail"),
        ("resume", None, False, "fail"),
        ("take", None, False, "fail"),
    ],
)
async def test_the_rule_by_what_the_hook_acted_on(
    acted, start, flagged, expected, memory_backend, world, hook_runs
) -> None:
    hook_runs.use(memory_backend.manager(run_max_attempts=3))
    if flagged:
        world.tools.flagged = {"probe"}
    assert await interrupted_disposition(_job(acted, start=start), attempts_used=3) == expected


@pytest.mark.parametrize(("acted", "start"), [("", None), ("start", _START)])
async def test_used_up_attempts_fail_even_a_rerun_or_a_replay(acted, start, memory_backend, world, hook_runs) -> None:
    hook_runs.use(memory_backend.manager(run_max_attempts=3))
    world.tools.flagged = {"probe"}
    job = _job(acted, start=start)
    assert await interrupted_disposition(job, attempts_used=4) == "fail"
    verdict = await attempt_module._disposition(job.acted, job.start, attempts_used=4)
    assert verdict.error == f"interrupted on attempt 3 of 3 and no attempts remain (acted={acted})"


# -- every path decides by the same rule ------------------------------------------------


@pytest.fixture
def rule_calls(monkeypatch) -> list[tuple[str, int]]:
    seen: list[tuple[str, int]] = []
    real = attempt_module._disposition

    async def _spy(acted, start, *, attempts_used):
        seen.append((acted, attempts_used))
        return await real(acted, start, attempts_used=attempts_used)

    monkeypatch.setattr(attempt_module, "_disposition", _spy)
    return seen


async def test_the_reclaim_passes_the_claimed_attempt_count(
    backend, world, hook_runs, clock, monkeypatch, rule_calls
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    spawn = runtime.spawn_attempt
    as_process(monkeypatch, "dead-host:1:aaaa")
    monkeypatch.setattr(runtime, "spawn_attempt", lambda job: None)
    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    as_process(monkeypatch, "live-host:2:bbbb")
    monkeypatch.setattr(runtime, "spawn_attempt", spawn)

    clock.advance(manager.settings.run_lease_seconds + 1)
    await sweep_tick()
    await hook_runs.drain()

    # The reclaim's claim counted attempt 2; the rule is asked with that count.
    assert rule_calls == [("", 2)]
    assert await get_run(manager, run_id) is None


async def test_the_shutdown_passes_the_next_attempt_count(backend, world, hook_runs, clock, rule_calls) -> None:
    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=0.05))
    await manager.register(hook("a"))
    world.interactions.parked_gate = asyncio.Event()

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await world.interactions.parked_started.wait()
    await stop_hook_runs_sweep()

    assert rule_calls == [("", 2)]
    assert (await held_run(manager, run_id)).status == "pending"


async def test_the_drain_passes_the_next_attempt_count(
    backend, world, tool_runs_store, hook_runs, clock, rule_calls
) -> None:
    from tai42_skeleton.operations.tool_runs.supervisor import drain_supervisors

    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    world.tools.flagged = {"probe"}
    gate, started = asyncio.Event(), asyncio.Event()
    world.tools.behave["probe"] = blocked(gate, started)

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await started.wait()
    await drain_supervisors(reason="r")
    await hook_runs.drain()

    assert rule_calls == [("start", 2)]
    run = await held_run(manager, run_id)
    assert (run.status, run.acted) == ("pending", "start")


# -- a replay is bounded ----------------------------------------------------------------


async def test_a_flagged_start_drained_every_attempt_runs_max_attempts_times(
    backend, world, tool_runs_store, hook_runs, clock, notices
) -> None:
    from tai42_skeleton.operations.tool_runs.supervisor import drain_supervisors

    manager = hook_runs.use(backend.manager(run_max_attempts=3))
    await manager.register(hook("a"))
    world.tools.flagged = {"probe"}
    started = asyncio.Event()
    world.tools.behave["probe"] = blocked(asyncio.Event(), started)

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    for _ in range(3):
        await started.wait()
        started.clear()
        await drain_supervisors(reason="the epoch retires")
        await hook_runs.drain()
        await sweep_tick()

    await hook_runs.drain()
    for _ in range(4):
        clock.advance(manager.settings.run_lease_seconds + 1)
        await sweep_tick()
        await hook_runs.drain()

    assert len(world.tools.calls) == 3
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.attempts, run.acted) == ("failed", 3, "start")
    assert "interrupted on attempt 3 of 3 and no attempts remain (acted=start)" in (run.last_error or "")
    assert [n for n in notices if run_id in n]


class _HolderDied(BaseException):
    """Ends an attempt task with no verdict, as the death of the process holding it would."""


async def test_a_flagged_start_whose_holder_dies_every_attempt_runs_max_attempts_times(
    backend, world, hook_runs, clock, notices
) -> None:
    manager = hook_runs.use(backend.manager(run_max_attempts=3))
    await manager.register(hook("a"))
    world.tools.flagged = {"probe"}

    async def _die(call):
        raise _HolderDied

    world.tools.behave["probe"] = _die

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await hook_runs.drain()
    for _ in range(6):
        run = await get_run(manager, run_id)
        assert run is not None
        assert run.attempts <= 4
        clock.advance(manager.settings.run_lease_seconds + 1)
        await sweep_tick()
        await hook_runs.drain()

    assert len(world.tools.calls) == 3
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted) == ("failed", "start")
    assert run.last_error == "interrupted on attempt 3 of 3 and no attempts remain (acted=start)"
    assert [n for n in notices if run_id in n]
