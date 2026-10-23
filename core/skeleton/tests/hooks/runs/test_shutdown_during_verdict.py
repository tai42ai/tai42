"""A shutdown cancel that arrives while an attempt records its verdict waits for that verdict.

The verdict is decided once the hook's work has ended: its success is recorded as a success, and
its failure is recorded with its own error and reported in full. The cancel then ends the attempt.
An attempt still running one more grace after its cancel is cancelled again, and logged.
"""

from __future__ import annotations

import asyncio
import logging

from tai42_skeleton.channels import notifications_sink
from tai42_skeleton.hooks.runs.loud import HOOK_RUN_FAILED_EVENT_TOPIC
from tai42_skeleton.hooks.runs.sweep import stop_hook_runs_sweep

from .conftest import all_runs, blocked, get_run, hook, metric

_GRACE = 0.05
_VERDICT_GRACE = 0.5


class _Held:
    """Holds a write until the stop has cancelled the attempt of run ``run_id``."""

    def __init__(self) -> None:
        self.run_id: str | None = None

    def _cancelled(self) -> bool:
        name = f"tai-hook-run-{self.run_id}"
        return any(task.get_name() == name and task.cancelling() for task in asyncio.all_tasks())

    def around(self, write):
        async def _held(*args, **kwargs):
            while not self._cancelled():
                await asyncio.sleep(0.005)
            return await write(*args, **kwargs)

        return _held


async def _refuse(call):
    raise ValueError("the tool refuses")


async def test_a_success_recorded_during_the_stop_stays_a_success(
    backend, world, hook_runs, clock, monkeypatch, caplog
):
    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=_VERDICT_GRACE))
    await manager.register(hook("a"))
    held = _Held()
    monkeypatch.setattr(manager, "_finish_run", held.around(manager._finish_run))
    interrupted_before = metric("tai42_hook_runs_interrupted_total", cause="shutdown")
    caplog.set_level(logging.WARNING)

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    held.run_id = run_id
    await stop_hook_runs_sweep()

    assert await get_run(manager, run_id) is None
    assert len(world.tools.calls) == 1
    assert metric("tai42_hook_runs_interrupted_total", cause="shutdown") == interrupted_before
    assert not [r for r in caplog.records if "interrupted" in r.getMessage()]


async def test_a_failure_recorded_during_the_stop_keeps_its_own_error(
    backend, world, hook_runs, clock, monkeypatch, notices
):
    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=_VERDICT_GRACE))
    await manager.register(hook("a"))
    world.tools.behave["probe"] = _refuse
    held = _Held()
    monkeypatch.setattr(manager, "_fail_run", held.around(manager._fail_run))

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    held.run_id = run_id
    await stop_hook_runs_sweep()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted, run.last_error) == ("failed", "start", "ValueError: the tool refuses")
    assert [n for n in notices if run_id in n]


async def test_a_failure_reported_during_the_stop_is_reported_in_full(
    backend, world, hook_runs, clock, monkeypatch, notices
):
    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=_VERDICT_GRACE))
    await manager.register(hook("a"))
    await manager.register(hook("watch", topic=HOOK_RUN_FAILED_EVENT_TOPIC))
    world.tools.behave["probe"] = _refuse
    held = _Held()
    monkeypatch.setattr(notifications_sink, "record_notification", held.around(notifications_sink.record_notification))

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    held.run_id = run_id
    await stop_hook_runs_sweep()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.last_error) == ("failed", "ValueError: the tool refuses")
    assert [n for n in notices if run_id in n]
    events = [r for r in await all_runs(manager) if r.topic == HOOK_RUN_FAILED_EVENT_TOPIC]
    assert [(r.hook, r.payload["run_id"], r.status) for r in events] == [("watch", run_id, "pending")]


async def _hang(*args, **kwargs):
    await asyncio.Event().wait()


async def test_a_release_that_hangs_after_the_cancel_is_cancelled_after_one_more_grace(
    backend, world, hook_runs, clock, monkeypatch, caplog
):
    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=_GRACE))
    await manager.register(hook("a"))
    world.interactions.parked_gate = asyncio.Event()
    monkeypatch.setattr(manager, "_release_run", _hang)
    caplog.set_level(logging.ERROR)

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await world.interactions.parked_started.wait()
    await asyncio.wait_for(stop_hook_runs_sweep(), timeout=5)

    assert [r for r in caplog.records if r.levelno == logging.ERROR and run_id in r.getMessage()]
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted) == ("running", "")


async def test_a_verdict_write_that_hangs_after_the_cancel_is_cancelled_after_one_more_grace(
    backend, world, hook_runs, clock, monkeypatch, caplog
):
    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=_GRACE))
    await manager.register(hook("a"))
    world.tools.behave["probe"] = _refuse
    monkeypatch.setattr(manager, "_fail_run", _hang)
    caplog.set_level(logging.ERROR)

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await asyncio.sleep(0)
    await asyncio.wait_for(stop_hook_runs_sweep(), timeout=5)

    assert [r for r in caplog.records if r.levelno == logging.ERROR and run_id in r.getMessage()]
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.acted) == ("running", "start")


async def test_the_stop_ends_one_grace_after_its_second_cancel_when_every_store_hangs(
    backend, world, tool_runs_store, hook_runs, clock, monkeypatch, caplog
):
    from tai42_skeleton.operations.tool_runs.store import ToolRunStore

    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=_GRACE))
    await manager.register(hook("a"))
    gate, started = asyncio.Event(), asyncio.Event()
    world.tools.behave["probe"] = blocked(gate, started)
    stores_back = asyncio.Event()

    async def _hang_until_back(*args, **kwargs):
        await stores_back.wait()

    caplog.set_level(logging.ERROR)
    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await started.wait()
    monkeypatch.setattr(ToolRunStore, "mark_terminal_if_running", _hang_until_back)
    monkeypatch.setattr(manager, "_fail_run", _hang_until_back)
    monkeypatch.setattr(manager, "_release_run", _hang_until_back)

    try:
        await asyncio.wait_for(stop_hook_runs_sweep(), timeout=5)
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        left = [message for message in errors if "left to the lease" in message]
        assert len(left) == 1, errors
        assert run_id in left[0], left
    finally:
        stores_back.set()
        gate.set()
