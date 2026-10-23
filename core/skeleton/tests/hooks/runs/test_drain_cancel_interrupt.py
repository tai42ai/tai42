"""Only a drain that marked the hook's tool supervisor interrupts an attempt; any other cancel fails it.

A drained start of a tool that declares crash-resume is released and replayed alone; every other
drained acted run is held as failed. A cancel of the attempt task itself takes the shutdown path.
A ``CancelledError`` the hook's own work raises, with no drain and no stop, fails the job at once.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from tai42_contract.tools import TOOL_EXTRA_IDEMPOTENCY_KEY

from tai42_skeleton.hooks.runs import attempt as attempt_module
from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs.sweep import sweep_tick

from .conftest import blocked, get_run, held_run, hook, metric, settle_loop

_REASON = "the serving epoch was retired by a config apply before the tool-run completed"
_SHUTDOWN = "the server is shutting down before the tool-run completed"
_EPOCH = 7


def _supervisor():
    import importlib

    return importlib.import_module("tai42_skeleton.operations.tool_runs.supervisor")


@pytest.fixture
def interrupts(monkeypatch) -> list[tuple[str, str | None]]:
    """Every ``_interrupt`` an attempt takes: its cause and the drain reason it saw."""
    seen: list[tuple[str, str | None]] = []
    real = attempt_module._Attempt._interrupt

    async def _spy(self, cause, *, reason=None):
        seen.append((cause, reason))
        return await real(self, cause, reason=reason)

    monkeypatch.setattr(attempt_module._Attempt, "_interrupt", _spy)
    return seen


@pytest.fixture
def admitting_epoch(monkeypatch) -> None:
    """Tag every supervisor enrolled in the test with the serving epoch that admitted its run."""
    from tai42_skeleton.app import epoch as epoch_module

    monkeypatch.setattr(epoch_module, "current_epoch_or_none", lambda: SimpleNamespace(number=_EPOCH))


async def _retire_epoch(monkeypatch) -> None:
    """Run the config-apply retire of the admitting epoch, with its client-pool steps stubbed."""
    from tai42_skeleton.app import epoch_retire
    from tai42_skeleton.app.epoch import Epoch

    async def _no_pool_drain(epoch, deadline):
        return None

    monkeypatch.setattr(epoch_retire, "drain_epoch", _no_pool_drain)
    monkeypatch.setattr(epoch_retire, "arm_retired_generation_certification", lambda old, retired: None)
    await epoch_retire.retire_epoch(Epoch(number=_EPOCH), _EPOCH, 1.0)


async def _drain(how: str, monkeypatch) -> None:
    if how == "epoch":
        await _supervisor().drain_supervisors(epoch=_EPOCH, reason=_REASON)
    elif how == "retire":
        await _retire_epoch(monkeypatch)
    else:
        await _supervisor()._drain_supervisors()


_DRAINS = [("epoch", _REASON), ("retire", _REASON), ("shutdown", _SHUTDOWN)]


async def _started_attempt(manager, world, *, flagged: bool):
    await manager.register(hook("a"))
    if flagged:
        world.tools.flagged = {"probe"}
    gate, started = asyncio.Event(), asyncio.Event()
    world.tools.behave["probe"] = blocked(gate, started)
    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await started.wait()
    return run_id, runtime._ATTEMPTS[run_id], gate


@pytest.mark.parametrize(("how", "reason"), _DRAINS)
async def test_a_drained_flagged_start_is_released_and_replayed_alone(
    how, reason, backend, world, tool_runs_store, hook_runs, clock, monkeypatch, interrupts, admitting_epoch
) -> None:
    manager = hook_runs.use(backend.manager())
    run_id, task, gate = await _started_attempt(manager, world, flagged=True)
    drained_before = metric("tai42_hook_runs_interrupted_total", cause="drain")

    await _drain(how, monkeypatch)
    await asyncio.gather(task, return_exceptions=True)

    assert interrupts == [("drain", reason)]
    assert not task.cancelled()
    assert task.exception() is None
    assert metric("tai42_hook_runs_interrupted_total", cause="drain") - drained_before == 1
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

    gate.set()
    await sweep_tick()
    await hook_runs.drain()

    assert await get_run(manager, run_id) is None
    first, replay = world.tools.calls
    assert replay.input == first.input
    assert replay.extras[TOOL_EXTRA_IDEMPOTENCY_KEY] == first.extras[TOOL_EXTRA_IDEMPOTENCY_KEY] == run_id
    assert len(world.interactions.parked_reads) == 1


@pytest.mark.parametrize(("how", "reason"), _DRAINS)
async def test_a_drained_unflagged_start_is_held_failed(
    how, reason, backend, world, tool_runs_store, hook_runs, clock, monkeypatch, interrupts, notices, admitting_epoch
) -> None:
    manager = hook_runs.use(backend.manager())
    run_id, task, _gate = await _started_attempt(manager, world, flagged=False)

    await _drain(how, monkeypatch)
    await asyncio.gather(task, return_exceptions=True)

    assert interrupts == [("drain", reason)]
    assert not task.cancelled()
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted, run.last_error_kind) == ("failed", "start", "unknown")
    assert run.last_error == f"interrupted after the hook acted (start): {reason}"
    assert [n for n in notices if run_id in n]

    clock.advance(manager.settings.run_lease_seconds + 1)
    await sweep_tick()
    await hook_runs.drain()
    assert len(world.tools.calls) == 1


async def test_a_cancel_of_the_attempt_itself_takes_the_shutdown_path_even_beside_a_drain(
    backend, world, tool_runs_store, hook_runs, clock, interrupts
) -> None:
    manager = hook_runs.use(backend.manager())
    run_id, task, _gate = await _started_attempt(manager, world, flagged=True)
    shutdown_before = metric("tai42_hook_runs_interrupted_total", cause="shutdown")
    drained_before = metric("tai42_hook_runs_interrupted_total", cause="drain")

    drain = asyncio.create_task(_supervisor().drain_supervisors(reason=_REASON))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, drain, return_exceptions=True)

    assert task.cancelled()
    assert interrupts == [("shutdown", None)]
    assert metric("tai42_hook_runs_interrupted_total", cause="shutdown") - shutdown_before == 1
    assert metric("tai42_hook_runs_interrupted_total", cause="drain") == drained_before
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted) == ("pending", "start")


# -- the hook's own CancelledError ------------------------------------------------------


async def _awaits_a_cancelled_future(call) -> None:
    future = asyncio.get_running_loop().create_future()
    future.cancel()
    await future


async def _gathers_a_cancelled_child(call) -> None:
    child = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    child.cancel()
    await asyncio.gather(child)


_OWN_CANCELS = [_awaits_a_cancelled_future, _gathers_a_cancelled_child]


async def _assert_failed_as_its_own_cancel(manager, world, hook_runs, clock, run_id) -> None:
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.last_error_kind, run.acted, run.attempts) == ("failed", "cancelled", "start", 1)
    assert run.last_error == (
        "the hook's work raised its own CancelledError; no drain and no stop cancelled this attempt (acted=start)"
    )
    for _ in range(3):
        clock.advance(manager.settings.run_lease_seconds + 1)
        await sweep_tick()
        await hook_runs.drain()
    assert len(world.tools.calls) == 1
    assert (await held_run(manager, run_id)).status == "failed"


@pytest.mark.parametrize("own_cancel", _OWN_CANCELS)
@pytest.mark.parametrize("flagged", [True, False])
async def test_a_tool_s_own_cancel_fails_the_job_at_once_with_the_store_on(
    own_cancel, flagged, backend, world, tool_runs_store, hook_runs, clock, interrupts
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    if flagged:
        world.tools.flagged = {"probe"}
    world.tools.behave["probe"] = own_cancel
    interrupted_before = {
        cause: metric("tai42_hook_runs_interrupted_total", cause=cause) for cause in ("drain", "shutdown")
    }

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await hook_runs.drain()

    await _assert_failed_as_its_own_cancel(manager, world, hook_runs, clock, run_id)
    assert interrupts == []
    assert {
        cause: metric("tai42_hook_runs_interrupted_total", cause=cause) for cause in ("drain", "shutdown")
    } == interrupted_before


# -- the tool-run store off -------------------------------------------------------------


async def test_with_the_store_off_a_drain_finds_no_hook_supervisor(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    run_id, task, gate = await _started_attempt(manager, world, flagged=True)

    await _supervisor().drain_supervisors(reason=_REASON)
    await settle_loop()
    assert not task.done()

    gate.set()
    await hook_runs.drain()
    assert await get_run(manager, run_id) is None
    assert len(world.tools.calls) == 1


@pytest.mark.parametrize("own_cancel", _OWN_CANCELS)
async def test_with_the_store_off_a_tool_s_own_cancel_fails_the_job_the_same_way(
    own_cancel, backend, world, hook_runs, clock, interrupts
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    world.tools.behave["probe"] = own_cancel

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await hook_runs.drain()

    await _assert_failed_as_its_own_cancel(manager, world, hook_runs, clock, run_id)
    assert interrupts == []
