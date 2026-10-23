"""A failing hook on the failed-run topic sends no further failed-run event, and is still reported."""

from __future__ import annotations

import logging

from tai42_skeleton.hooks.runs.loud import HOOK_RUN_FAILED_EVENT_TOPIC

from .conftest import all_runs, hook


async def test_a_failing_failed_run_hook_does_not_feed_itself(
    backend, world, hook_runs, clock, notices, caplog
) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("watch", topic=HOOK_RUN_FAILED_EVENT_TOPIC))
    caplog.set_level(logging.ERROR, logger="tai42_skeleton.hooks.runs.loud")

    async def _refuse(call):
        raise ValueError("the watcher refuses")

    world.tools.behave["probe"] = _refuse

    [run_id] = (await manager.on_event(HOOK_RUN_FAILED_EVENT_TOPIC, {"run_id": "x"})).run_ids
    await hook_runs.drain()

    runs = await all_runs(manager)
    assert [run.id for run in runs] == [run_id]
    assert runs[0].status == "failed"
    assert len(world.tools.calls) == 1
    assert [r for r in caplog.records if run_id in r.getMessage() and r.levelno == logging.ERROR]
    assert [n for n in notices if run_id in n]
