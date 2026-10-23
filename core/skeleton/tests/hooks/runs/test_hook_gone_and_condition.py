"""Each attempt reads the hook as it is registered now: a gone hook fails, its condition decides afresh."""

from __future__ import annotations

import logging

from tai42_contract.template import TemplatedText

from tai42_skeleton.hooks.runs.sweep import sweep_tick

from .conftest import all_runs, get_run, held_run, hook, operator


async def test_a_hook_unregistered_before_its_attempt_fails_not_found(
    backend, world, hook_runs, clock, notices
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    hook_runs.off_serving_loop()
    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await manager.unregister("a")

    hook_runs.back_on_serving_loop()
    await sweep_tick()
    await hook_runs.drain()

    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.last_error_kind, run.attempts, run.acted) == ("failed", "not_found", 1, "")
    assert world.tools.calls == []
    assert [n for n in notices if run_id in n]


async def test_a_false_condition_deletes_the_run_without_the_tool(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a", condition=TemplatedText(content=".n > 5")))

    await manager.on_event("t", {"n": 1})
    await hook_runs.drain()

    assert await all_runs(manager) == []
    assert world.tools.calls == []


async def test_a_condition_that_raises_fails_the_run_at_once_and_an_operator_retries_it(
    backend, world, hook_runs, clock, notices, caplog, monkeypatch
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a", condition=TemplatedText(content=".n | tonumber > 0")))
    caplog.set_level(logging.ERROR, logger="tai42_skeleton.hooks.runs.loud")

    [run_id] = (await manager.on_event("t", {"n": "not a number"})).run_ids
    await hook_runs.drain()
    clock.advance(manager.settings.run_retry_cap_seconds)
    await sweep_tick()
    await hook_runs.drain()

    # A jq evaluation error on the run's own payload is bad input: no retry can change it.
    run = await held_run(manager, run_id)
    assert (run.status, run.attempts, run.acted, run.last_error_kind) == ("failed", 1, "", "bad_input")
    failed = await manager.list_hook_runs("failed", 10, None)
    assert [item.id for item in failed.items] == [run_id]
    assert [r for r in caplog.records if r.levelno == logging.ERROR and run_id in r.getMessage()]
    assert [n for n in notices if run_id in n]
    assert world.tools.calls == []

    await manager.register(hook("a", condition=TemplatedText(content=".n | length > 0")))
    await operator(monkeypatch, manager).retry_hook_run(run_id)
    await hook_runs.drain()

    assert await all_runs(manager) == []
    assert [call.name for call in world.tools.calls] == ["probe"]


async def test_a_hook_re_registered_between_attempts_is_used_by_the_next(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    world.interactions.parked_faults = [ConnectionError("store down")]

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await hook_runs.drain()
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, run.last_error_kind) == ("pending", "unavailable")

    await manager.register(hook("a", tool="other", condition=TemplatedText(content=".n > 0")))
    clock.advance(manager.settings.run_retry_base_seconds)
    await sweep_tick()
    await hook_runs.drain()

    assert await all_runs(manager) == []
    assert [call.name for call in world.tools.calls] == ["other"]


async def test_a_condition_made_false_between_attempts_ends_the_run(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    world.interactions.parked_faults = [ConnectionError("store down")]

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await hook_runs.drain()
    assert (await held_run(manager, run_id)).status == "pending"

    await manager.register(hook("a", condition=TemplatedText(content=".n > 5")))
    clock.advance(manager.settings.run_retry_base_seconds)
    await sweep_tick()
    await hook_runs.drain()

    assert await all_runs(manager) == []
    assert world.tools.calls == []
