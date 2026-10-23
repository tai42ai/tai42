"""A webhook delivery id is claimed in the same atomic step as its runs are kept."""

from __future__ import annotations

import pytest

from tai42_skeleton.hooks.runs.models import ReplayClaim

from .conftest import all_runs, hook


async def test_a_second_accept_of_the_same_delivery_writes_no_job(backend, world, hook_runs, clock) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))

    first = await manager.on_event("t", {"n": 1}, replay_claim=ReplayClaim("d-1", 300))
    await hook_runs.drain()
    hook_runs.off_serving_loop()
    second = await manager.on_event("t", {"n": 1}, replay_claim=ReplayClaim("d-1", 300))

    assert first.status == "accepted"
    assert len(first.run_ids) == 1
    assert second.status == "already_seen"
    assert second.run_ids == ()
    # The first run finished and was deleted; the replay kept nothing.
    assert await all_runs(manager) == []
    assert len(world.tools.calls) == 1


def _break_the_job_write(backend, manager, monkeypatch) -> None:
    """Make the job write fail inside the store's atomic accept step."""
    if backend.kind == "memory":

        def _raise(run_id, score):
            raise OSError("the run store refused the write")

        monkeypatch.setattr(manager, "_index_due", _raise)


async def test_a_failed_job_write_leaves_no_seen_key(backend, world, hook_runs, clock, monkeypatch) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    hook_runs.off_serving_loop()
    if backend.kind == "redis":
        # A due index of the wrong type makes the accept script's ZADD raise after the job hash
        # write and before the claim is taken.
        await backend.redis.set(manager.settings.hook_runs_due_key, "not-a-zset")
    else:
        _break_the_job_write(backend, manager, monkeypatch)

    with pytest.raises(Exception, match=r"WRONGTYPE|refused the write"):
        await manager.on_event("t", {"n": 1}, replay_claim=ReplayClaim("d-1", 300))

    seen_key = manager.settings.webhook_seen_key("t", "d-1")
    if backend.kind == "redis":
        assert await backend.redis.exists(seen_key) == 0
        await backend.redis.delete(manager.settings.hook_runs_due_key)
    else:
        assert seen_key not in manager._webhook_seen
        monkeypatch.undo()
        hook_runs.use(manager)
        hook_runs.off_serving_loop()

    again = await manager.on_event("t", {"n": 1}, replay_claim=ReplayClaim("d-1", 300))
    assert again.status == "accepted"
    assert len(again.run_ids) == 1
    assert again.run_ids[0] in {run.id for run in await all_runs(manager)}
