"""A run that fails before its hook acts on every attempt is held as failed, loudly, once its attempts are used."""

from __future__ import annotations

import logging

from tai42_skeleton.hooks.runs.loud import HOOK_RUN_FAILED_EVENT_TOPIC
from tai42_skeleton.hooks.runs.sweep import sweep_tick

from .conftest import get_run, hook, metric


async def test_attempts_used_up_fail_the_run_loudly(backend, world, hook_runs, clock, notices, caplog) -> None:
    manager = hook_runs.use(backend.manager(run_max_attempts=3))
    await manager.register(hook("a"))
    await manager.register(hook("watch", topic=HOOK_RUN_FAILED_EVENT_TOPIC, tool="watcher"))
    world.interactions.parked_faults = [ConnectionError("store down") for _ in range(3)]
    failed_before = metric("tai42_hook_runs_failed_total")
    caplog.set_level(logging.ERROR, logger="tai42_skeleton.hooks.runs.loud")

    [run_id] = (await manager.on_event("t", {"n": 1})).run_ids
    await hook_runs.drain()
    for wait in (10, 20):
        clock.advance(wait)
        await sweep_tick()
        await hook_runs.drain()

    run = await get_run(manager, run_id)
    assert run is not None
    assert run.status == "failed"
    assert run.attempts == 3
    assert run.acted == ""
    assert run.last_error_kind == "unavailable"
    assert world.tools.of("probe") == []
    assert metric("tai42_hook_runs_failed_total") == failed_before + 1
    [record] = [r for r in caplog.records if run_id in r.getMessage()]
    assert record.levelno == logging.ERROR
    assert "acted=" in record.getMessage()
    assert [n for n in notices if run_id in n]
    # The failed-run event was kept as a run of the hook on its topic, which ran once.
    assert len(world.tools.of("watcher")) == 1
