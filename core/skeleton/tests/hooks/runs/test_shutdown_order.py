"""The lifespan shutdown stops the hook runs first, while the tool supervisors and the clients are live.

``stop_hook_runs_sweep`` finishes before the credential refreshes close and before the first
shutdown handler drains the tool supervisors, so a hook's tool that ends inside the grace is
recorded as succeeded and the drain finds nothing of it to cancel.
"""

from __future__ import annotations

import asyncio

from tai42_contract.app import tai42_app

from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs import sweep as sweep_module
from tai42_skeleton.manifest import Manifest

from .conftest import blocked, get_run, hook


def _spy_the_shutdown(monkeypatch) -> tuple[list[str], list[int]]:
    """Record the order of the three shutdown steps, and how many supervisors the drain found live."""
    import importlib

    from tai42_skeleton.connectors.runtime import refresh_owner

    supervisor = importlib.import_module("tai42_skeleton.operations.tool_runs.supervisor")
    order: list[str] = []
    live_at_drain: list[int] = []
    stop = sweep_module.stop_hook_runs_sweep
    aclose = refresh_owner.aclose
    drain = supervisor.drain_supervisors

    async def _stop():
        order.append("stop_hook_runs_sweep started")
        await stop()
        order.append("stop_hook_runs_sweep finished")

    async def _aclose(*args, **kwargs):
        order.append("refresh_owner.aclose")
        return await aclose(*args, **kwargs)

    async def _drain(*args, **kwargs):
        order.append("drain_supervisors")
        live_at_drain.append(sum(1 for task in supervisor._SUPERVISORS if not task.done()))
        return await drain(*args, **kwargs)

    monkeypatch.setattr(sweep_module, "stop_hook_runs_sweep", _stop)
    monkeypatch.setattr(refresh_owner, "aclose", _aclose)
    monkeypatch.setattr(supervisor, "drain_supervisors", _drain)
    return order, live_at_drain


async def test_the_hook_runs_stop_before_the_refreshes_close_and_the_supervisors_drain(
    memory_backend, world, hook_runs, monkeypatch
) -> None:
    from tai42_skeleton.app import instance

    hook_runs.use(memory_backend.manager())
    order, _live = _spy_the_shutdown(monkeypatch)

    async with instance.app.app_context(Manifest.model_validate({})):
        pass

    assert order[:4] == [
        "stop_hook_runs_sweep started",
        "stop_hook_runs_sweep finished",
        "refresh_owner.aclose",
        "drain_supervisors",
    ]


async def test_a_hook_s_tool_ending_inside_the_grace_is_recorded_succeeded(
    backend, world, tool_runs_store, hook_runs, clock, monkeypatch
) -> None:
    from tai42_skeleton.app import instance

    manager = hook_runs.use(backend.manager(run_shutdown_grace_seconds=5.0))
    order, live_at_drain = _spy_the_shutdown(monkeypatch)
    gate, started = asyncio.Event(), asyncio.Event()
    world.tools.behave["probe"] = blocked(gate, started)

    async with instance.app.app_context(Manifest.model_validate({})):
        # The boot binds the process app; the hook's tool runs on the fake app's tools.
        monkeypatch.setattr(tai42_app, "_impl", world)
        await manager.register(hook("a"))
        [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
        await started.wait()
        asyncio.get_running_loop().call_later(0.05, gate.set)

    assert order.index("stop_hook_runs_sweep finished") < order.index("drain_supervisors")
    assert live_at_drain == [0]
    assert await get_run(manager, run_id) is None
    [record] = await tool_runs_store.ops.list_tool_runs("probe")
    assert record["status"] == "succeeded"
    assert len(world.tools.calls) == 1


async def test_an_accept_after_the_stop_writes_its_job_due_now_and_spawns_no_attempt(
    backend, world, hook_runs, clock, monkeypatch
) -> None:
    from tai42_skeleton.app import instance

    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))

    async with instance.app.app_context(Manifest.model_validate({})):
        pass

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids

    assert runtime.attempt_count() == 0
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.attempts, run.claimed_by, run.next_attempt_at) == ("pending", 0, None, clock.t)
    assert world.tools.calls == []


async def test_an_accept_while_the_sweep_is_cancelled_spawns_no_attempt(
    backend, world, hook_runs, clock, monkeypatch
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    cancelling, release = asyncio.Event(), asyncio.Event()

    async def _sweep_cleaning_up() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
            raise

    sweep = asyncio.create_task(_sweep_cleaning_up())
    await asyncio.sleep(0)
    monkeypatch.setattr(sweep_module, "_sweep_task", sweep)
    stop = asyncio.create_task(sweep_module.stop_hook_runs_sweep())
    await cancelling.wait()

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids

    assert runtime.attempt_count() == 0
    release.set()
    await stop
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.attempts, run.claimed_by, run.next_attempt_at) == ("pending", 0, None, clock.t)
    assert world.tools.calls == []
